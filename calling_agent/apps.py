from django.apps import AppConfig


class CallingAgentConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'calling_agent'

    def ready(self):
        import calling_agent.signals  # noqa
