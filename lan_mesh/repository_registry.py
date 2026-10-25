"""Ordinary aggregate repositories; SQL stays below the business service boundary."""

class Repository:
    """Refer to the database owner, never cache its thread-local connection."""
    def __init__(self, database: object):
        self.database = database


class Repositories:
    """Compose explicit aggregate repositories over one database owner."""
    def __init__(self, database: object):
        from .repository_hosts import HostsRepository
        self.hosts = HostsRepository(database)
        from .repository_agents import AgentsRepository
        self.agents = AgentsRepository(database)
        from .repository_tasks import TasksRepository
        self.tasks = TasksRepository(database)
        from .repository_projects import ProjectsRepository
        self.projects = ProjectsRepository(database)
        from .repository_skills import SkillsRepository
        self.skills = SkillsRepository(database)
        from .repository_users import UsersRepository
        self.users = UsersRepository(database)
        from .repository_teams import TeamsRepository
        self.teams = TeamsRepository(database)
        from .repository_conversations import ConversationsRepository
        self.conversations = ConversationsRepository(database)
        from .repository_checkpoints import CheckpointsRepository
        self.checkpoints = CheckpointsRepository(database)
        from .repository_resources import ResourcesRepository
        self.resources = ResourcesRepository(database)
        from .repository_metrics import MetricsRepository
        self.metrics = MetricsRepository(database)
        from .repository_operations import OperationsRepository
        self.operations = OperationsRepository(database)
