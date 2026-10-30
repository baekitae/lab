import io,sqlite3,tempfile,unittest,zipfile
from pathlib import Path
from data_paths import data_directory,migrate_legacy
from update_app import update

class PersistenceTests(unittest.TestCase):
    def test_windows_path_independent_of_download_folder_and_explicit_override(self):
        env={'LOCALAPPDATA':'C:/Users/Test/AppData/Local'}
        self.assertEqual(data_directory('/old',env,'nt'),data_directory('/new',env,'nt'))
        self.assertEqual(data_directory('/new',{'PL_DATA_DIR':'/custom'},'nt'),Path('/custom'))
        with self.assertRaises(ValueError):data_directory('/new',{},'nt')
    def test_migration_preserves_cipher_and_database_and_never_merges(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);old=root/'old';old.mkdir();new=root/'stable'
            (old/'credentials.dpapi').write_bytes(b'fake-encrypted-fixture')
            with sqlite3.connect(old/'league.sqlite3') as db:db.execute('create table example(value)');db.execute('insert into example values (42)')
            self.assertTrue(migrate_legacy(old,new))
            self.assertEqual((new/'credentials.dpapi').read_bytes(),b'fake-encrypted-fixture')
            self.assertTrue((old/'credentials.dpapi').exists())
            with sqlite3.connect(new/'league.sqlite3') as db:self.assertEqual(db.execute('select value from example').fetchone()[0],42)
            (old/'credentials.dpapi').write_bytes(b'changed-old')
            self.assertFalse(migrate_legacy(old,new))
            self.assertEqual((new/'credentials.dpapi').read_bytes(),b'fake-encrypted-fixture')
    def test_migration_bad_database_does_not_create_target(self):
        with tempfile.TemporaryDirectory() as d:
            old=Path(d)/'old';old.mkdir();target=Path(d)/'stable'
            (old/'league.sqlite3').write_bytes(b'invalid database')
            with self.assertRaises(sqlite3.DatabaseError):migrate_legacy(old,target)
            self.assertFalse(target.exists());self.assertTrue((old/'league.sqlite3').exists())
    def archive(self,extra=None):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:
            for n in ('server.py','settings_store.py','data_paths.py','bootstrap.py'):z.writestr('lab-main/'+n,b'# new code\n')
            for name,content in (extra or {}).items():z.writestr(name,content)
        return buf.getvalue()
    def test_updater_keeps_runtime_data_and_saves_old_code(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'data').mkdir();(root/'data/credentials.dpapi').write_bytes(b'original-vault');(root/'server.py').write_bytes(b'old code')
            update(root,self.archive({'lab-main/data/credentials.dpapi':b'bad overwrite','lab-main/.env':b'bad env'}))
            self.assertEqual((root/'data/credentials.dpapi').read_bytes(),b'original-vault')
            self.assertFalse((root/'.env').exists())
            self.assertEqual(next((root/'code_backups').glob('*/server.py')).read_bytes(),b'old code')
    def test_updater_rejects_path_traversal_before_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):update(d,self.archive({'lab-main/../escape.py':b'bad'}))
            self.assertFalse((Path(d)/'server.py').exists())

    def test_updater_rolls_back_code_if_a_write_fails(self):
        from unittest.mock import patch
        import os
        original=os.replace;calls=[]
        def fail_second(src,dest):
            calls.append(str(dest))
            if len(calls)==2:raise OSError('fixture write failure')
            return original(src,dest)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'server.py').write_bytes(b'old code')
            with patch('update_app.os.replace',side_effect=fail_second):
                with self.assertRaises(OSError):update(root,self.archive())
            self.assertEqual((root/'server.py').read_bytes(),b'old code')
            self.assertFalse((root/'settings_store.py').exists())
