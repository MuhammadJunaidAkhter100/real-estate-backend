from django.apps import AppConfig


class ProjectsConfig(AppConfig):
    name = 'projects'

    def ready(self):
        # Wire up the Unit post_save / post_delete signals that keep the
        # cached unit counts on Project in sync.
        from projects import signals  # noqa: F401

