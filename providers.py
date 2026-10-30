"""Server-only provider adapters. Never log credentials or provider error bodies."""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

API_ROOT = 'https://generativelanguage.googleapis.com/v1beta'
MAX_OUTPUT_TOKENS = 4096


class ProviderError(RuntimeError):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class Report:
    data: dict
    prompt_tokens: int
    output_tokens: int
    model: str
    usage_known: bool


class ProviderAdapter(Protocol):
    def generate_structured_report(self, system: str, context: dict, schema: dict) -> Report: ...
    def estimate_cost(self, system: str, context: dict, schema: dict) -> float: ...
    def list_models(self) -> list[str]: ...
    def health_check(self) -> dict: ...


def valid_model(model):
    if isinstance(model,str):
        model=model.strip()
        friendly=re.fullmatch(r'Gemini\s+(\d+(?:\.\d+)?)\s+(Flash(?:\s+Lite)?|Pro)',model,re.IGNORECASE)
        if friendly:model='gemini-'+friendly.group(1)+'-'+friendly.group(2).lower().replace(' ','-')
    if not isinstance(model, str) or not re.fullmatch(r'gemini-[A-Za-z0-9._-]{1,100}', model):
        raise ValueError('실제 사용 가능한 Gemini 모델 ID를 설정하세요 (gemini- 접두사).')
    return model


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # API credentials must never be forwarded to another host through redirects.
        return None


class GeminiAdapter:
    def __init__(self, key, model, input_rate, output_rate, opener=None):
        if not key:
            raise ValueError('역할별 API 키가 필요합니다.')
        self._key = key
        self.model = valid_model(model)
        self.input_rate = input_rate
        self.output_rate = output_rate
        self._opener = opener or urllib.request.build_opener(NoRedirect()).open

    @staticmethod
    def map_error(status):
        mapping = {
            400: ('invalid_request', 'Gemini가 모델·요청 형식을 거부했습니다. 모델 기능과 설정을 확인하세요.'),
            401: ('authentication', 'Gemini 인증에 실패했습니다. 해당 역할의 키를 확인하세요.'),
            403: ('access_denied', 'Gemini 프로젝트 권한 또는 네트워크 접근이 거부되었습니다.'),
            404: ('model_unavailable', '해당 Gemini 모델이 없거나 이 프로젝트에서 사용할 수 없습니다.'),
            429: ('rate_limit', '해당 프로젝트 호출 한도를 초과했습니다. 다른 키로 우회하지 않습니다.'),
        }
        code, message = mapping.get(status, ('provider_failure', f'Gemini 서비스 오류 (HTTP {status}).'))
        return ProviderError(code, message, retryable=status == 429)

    def detailed_error(self,status,raw):
        error=self.map_error(status)
        # Classify only known indicators. Never return provider text, metadata or keys.
        try:
            data=json.loads(raw);body=data.get('error',{})
            message=body.get('message','')
            if not isinstance(message,str):message=''
            details=body.get('details',[])
            reasons={d.get('reason') for d in details if isinstance(d,dict)} if isinstance(details,list) else set()
        except (ValueError,TypeError,AttributeError):return error
        rules=[
            ('api_key_invalid',bool(reasons&{'API_KEY_INVALID','API_KEY_EXPIRED'}) or bool(re.search(r'(?i)api.?key.*(?:not valid|invalid|expired)',message)),'Google가 이 역할의 API 키를 유효하지 않거나 만료된 키로 판단했습니다. 다른 역할의 키는 유지하고 해당 키의 상태·API 제한을 확인하세요.'),
            ('region_unsupported','FAILED_PRECONDITION'==body.get('status') and bool(re.search(r'(?i)location|region|country',message)),'현재 접속 지역에서 Gemini 요청을 허용하지 않았습니다. Google의 제공 지역과 프로젝트 정책을 확인하세요.'),
            ('thinking_unsupported',bool(re.search(r'(?i)thinking[_ ]?(?:config|level|budget)',message)),'선택한 모델이 요청의 thinking 설정을 거부했습니다. 모델 ID와 모델별 thinking 지원을 확인해야 합니다.'),
            ('schema_unsupported',bool(re.search(r'(?i)response[_ ]?(?:schema|jsonschema|mime[_ ]?type)|(?:generation_config|generationConfig).*schema',message)),'선택한 모델이 구조화 JSON 스키마 설정을 거부했습니다. 해당 모델의 구조화 출력 지원과 요청 스키마를 확인해야 합니다.'),
            ('temperature_unsupported',bool(re.search(r'(?i)temperature',message)),'선택한 모델이 temperature 설정을 거부했습니다. 모델별 생성 옵션 호환성 수정이 필요합니다.'),
        ]
        if status==400:
            for code,matched,explanation in rules:
                if matched:return ProviderError(code,explanation+' (HTTP 400 · 모델 '+self.model+')')
        return ProviderError(error.code,str(error)+' (HTTP '+str(status)+' · 모델 '+self.model+')',error.retryable)

    def _request(self, path, payload=None):
        headers = {'x-goog-api-key': self._key, 'Content-Type': 'application/json'}
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() if payload is not None else None
        req = urllib.request.Request(API_ROOT + path, data=body, headers=headers)
        try:
            with self._opener(req, timeout=180 if ':generateContent' in path else 30) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ProviderError('response_too_large', 'Gemini 응답 크기 한도를 초과했습니다.')
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            # Inspect a bounded body for known categories only; discard the original.
            code = exc.code
            try:raw=exc.read(65537)
            except OSError:raw=b''
            finally:exc.close()
            raise self.detailed_error(code,raw if len(raw)<=65536 else b'') from None
        except (urllib.error.URLError, TimeoutError, socket.timeout):
            raise ProviderError('network_uncertain', 'Gemini 연결이 끊겼습니다. 중복 과금 방지를 위해 자동 재전송하지 않습니다.') from None
        except (ValueError, UnicodeError):
            raise ProviderError('invalid_json', 'Gemini 응답을 JSON으로 읽을 수 없습니다.') from None

    def estimate_cost(self, system, context, schema):
        # Conservative local reservation, not a provider billing guarantee.
        # Includes generous room for template/schema overhead and thinking tokens.
        input_bound = len(json.dumps([system, context, schema], ensure_ascii=False).encode()) + 4096
        return (input_bound * self.input_rate + (MAX_OUTPUT_TOKENS + 32768) * self.output_rate) / 1_000_000

    def generate_structured_report(self, system, context, schema):
        payload = {
            'systemInstruction': {'parts': [{'text': system}]},
            'contents': [{'role': 'user', 'parts': [{'text': json.dumps(context, ensure_ascii=False, allow_nan=False)}]}],
            'generationConfig': {'responseMimeType': 'application/json', 'responseSchema': schema,
                                 'maxOutputTokens': MAX_OUTPUT_TOKENS, 'temperature': .3},
        }
        if re.match(r'gemini-[3-9](?:[.-]|$)',self.model):
            payload['generationConfig']['thinkingConfig']={'thinkingLevel':'LOW'}
        response = self._request(f'/models/{self.model}:generateContent', payload)
        candidates = response.get('candidates', []) if isinstance(response, dict) else []
        if not candidates or candidates[0].get('finishReason') != 'STOP':
            raise ProviderError('incomplete_response', 'Gemini가 완성된 보고서를 반환하지 않았습니다 (차단 또는 토큰 한도).')
        try:
            parts = candidates[0]['content']['parts']
            text = ''.join(part.get('text', '') for part in parts if not part.get('thought'))
            result = json.loads(text)
            if not isinstance(result, dict):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ProviderError('invalid_report', 'Gemini 구조화 보고서 형식이 올바르지 않습니다.') from None
        usage = response.get('usageMetadata', {})
        known = all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('promptTokenCount', 'candidatesTokenCount'))
        thinking = usage.get('thoughtsTokenCount', 0)
        if type(thinking) is not int or thinking < 0:
            known = False
            thinking = 0
        return Report(result, usage.get('promptTokenCount', 0) if known else 0,
                      usage.get('candidatesTokenCount', 0) + thinking if known else 0,
                      response.get('modelVersion', self.model), known)

    def list_models(self):
        models, token = [], ''
        # Avoid silently truncating the model list; bounded pagination.
        for _ in range(20):
            query = '?pageSize=100' + ('&pageToken=' + urllib.parse.quote(token, safe='') if token else '')
            response = self._request('/models' + query)
            models.extend(m['name'].removeprefix('models/') for m in response.get('models', [])
                          if 'generateContent' in m.get('supportedGenerationMethods', []))
            token = response.get('nextPageToken', '')
            if not token:
                return models
        raise ProviderError('model_list_incomplete', '모델 목록의 페이지 한도를 초과했습니다.')

    def health_check(self):
        available = self.model in self.list_models()
        return {'provider': 'gemini', 'model': self.model, 'available': available,
                'note': '모델 목록 접근 확인이며 생성 호출·무료 자격·RPM/TPM/RPD 한도 검증은 아닙니다.'}
