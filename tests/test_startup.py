import contextlib
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import gift_flipper_app as app
from settings import Settings, load_settings
from tk_support import prepare_tk_environment

TOKEN = '12345:' + 'x' * 24
class TclError(Exception): pass

class StartupTests(unittest.TestCase):
    def test_windows_venv_uses_base_python_libraries(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name, marker in [('tcl8.6', 'init.tcl'), ('tk8.6', 'tk.tcl')]:
                directory = root / 'tcl' / name
                directory.mkdir(parents=True)
                (directory / marker).write_text('# fixture')
            env = {'TCL_LIBRARY': 'D:/broken/tcl', 'TK_LIBRARY': 'D:/broken/tk'}
            prepare_tk_environment(platform='win32', prefixes=[root], environ=env,
                                   tcl_version='8.6', tk_version='8.6')
            self.assertEqual(env['TCL_LIBRARY'], str(root / 'tcl/tcl8.6'))
            self.assertEqual(env['TK_LIBRARY'], str(root / 'tcl/tk8.6'))

    def test_stale_env_removed_without_inventing_paths(self):
        with tempfile.TemporaryDirectory() as d:
            env = {'TCL_LIBRARY': d + '/missing', 'TK_LIBRARY': d + '/missing2', 'OTHER': 'keep'}
            prepare_tk_environment(platform='win32', prefixes=[d], environ=env,
                                   tcl_version='8.6', tk_version='8.6')
            self.assertEqual(env, {'OTHER': 'keep'})

    def test_console_setup_retries_invalid_id_and_keeps_filters(self):
        stream = io.StringIO()
        ids = iter(['-100', '12345'])
        with tempfile.TemporaryDirectory() as d, patch.object(app, 'BASE_DIR', Path(d)), contextlib.redirect_stdout(stream):
            cfg = Settings(dm_collections='BerryBox', max_model_rarity=0.2)
            app.setup_console(cfg, input_fn=lambda _: next(ids), token_fn=lambda _: TOKEN)
            saved = load_settings(Path(d) / 'config.json')
            self.assertEqual(saved.bot_token, TOKEN)
            self.assertEqual(saved.chat_id, '12345')
            self.assertEqual(saved.max_model_rarity, 0.2)
            self.assertEqual(saved.dm_collections, 'BerryBox')
            self.assertEqual(cfg.bot_token, '')
        self.assertNotIn(TOKEN, stream.getvalue())

    def test_configured_bot_does_not_prompt_or_rewrite(self):
        cfg = Settings(bot_token=TOKEN, chat_id='12345')
        with patch.object(app, 'save_settings') as save:
            def unexpected(_): self.fail('Unexpected credential prompt')
            self.assertIs(app.setup_console(cfg, unexpected, unexpected), cfg)
            save.assert_not_called()

    def test_tcl_failure_falls_back_to_configured_console(self):
        fake_gui = types.SimpleNamespace(HunterGUI=lambda *args: (_ for _ in ()).throw(TclError('missing init.tcl')))
        configured = Settings(bot_token=TOKEN, chat_id='12345')
        with patch.dict(sys.modules, {'gui': fake_gui}), patch('tk_support.prepare_tk_environment'), patch.object(sys.stdin, 'isatty', return_value=True), patch.object(app, 'setup_console', return_value=configured) as setup, patch.object(app, 'console', return_value=0) as console, contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.run_desktop(Settings()), 0)
            setup.assert_called_once()
            console.assert_called_once_with(configured, autostart=True)

    def test_noninteractive_tcl_failure_explains_repair(self):
        fake_gui = types.SimpleNamespace(HunterGUI=lambda *args: (_ for _ in ()).throw(TclError('missing init.tcl')))
        out = io.StringIO()
        with patch.dict(sys.modules, {'gui': fake_gui}), patch('tk_support.prepare_tk_environment'), patch.object(sys.stdin, 'isatty', return_value=False), patch.object(app, 'console') as console, contextlib.redirect_stderr(out):
            self.assertEqual(app.run_desktop(Settings()), 1)
        console.assert_not_called()
        self.assertIn('START_TELEGRAM.bat', out.getvalue())
        self.assertIn('python -m tkinter', out.getvalue())

    def test_headless_does_not_import_tkinter(self):
        with tempfile.TemporaryDirectory() as d, patch.object(app, 'BASE_DIR', Path(d)), patch.object(sys, 'argv', ['app', '--headless']), patch.dict(sys.modules, {'gui': None, 'tkinter': None, '_tkinter': None}), patch.object(app, 'console', return_value=0) as console:
            self.assertEqual(app.main(), 0)
            console.assert_called_once()
