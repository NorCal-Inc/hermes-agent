"""Import-safety tests for the Discord gateway adapter."""

import builtins
import importlib
import sys


class TestDiscordImportSafety:
    def test_module_imports_even_when_discord_dependency_is_missing(self, monkeypatch):
        original_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "discord" or name.startswith("discord."):
                raise ImportError("discord unavailable for test")
            return original_import(name, globals, locals, fromlist, level)

        # The re-import below also rebinds package attributes
        # (plugins.platforms.discord, plugins.platforms.discord.adapter).
        # sys.modules is restored at teardown, but those attributes are not,
        # so later "import plugins.platforms.discord.adapter as x" would get
        # the discord-less copy.  Record them so monkeypatch restores them.
        import plugins.platforms as platforms_pkg
        import plugins.platforms.discord as discord_pkg
        import plugins.platforms.discord.adapter  # noqa: F401
        monkeypatch.setattr(platforms_pkg, "discord", discord_pkg)
        monkeypatch.setattr(discord_pkg, "adapter", discord_pkg.adapter)

        # Purge the cached module so the import below actually re-runs the
        # module body with discord.py simulated-missing.
        monkeypatch.delitem(sys.modules, "plugins.platforms.discord.adapter", raising=False)
        monkeypatch.delitem(sys.modules, "plugins.platforms.discord", raising=False)
        monkeypatch.setattr(builtins, "__import__", fake_import)

        module = importlib.import_module("plugins.platforms.discord.adapter")

        assert module.DISCORD_AVAILABLE is False
        assert module.discord is None
