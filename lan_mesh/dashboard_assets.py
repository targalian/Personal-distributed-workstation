"""Dashboard domain assembly preserving classic JavaScript globals and source order."""

from pathlib import Path

DASHBOARD_MODULES = ('auth.js', 'shell.js', 'hosts_tasks.js', 'agents_tools.js', 'resources.js', 'projects.js', 'chat.js', 'conversations.js', 'pm_teams.js', 'workspace.js', 'station.js', 'skills_bot.js', 'p2p.js', 'dag.js', 'runtime.js', 'bootstrap.js')

def load_dashboard_script() -> str:
    """Assemble trusted domain files into one script so legacy handlers keep their scope."""
    directory = Path(__file__).parent / 'web' / 'static' / 'dashboard'
    return ''.join((directory/name).read_text(encoding='utf-8') for name in DASHBOARD_MODULES)
