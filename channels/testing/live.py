from django.conf import settings
from django.db import connections
from django.test.testcases import LiveServerTestCase, LiveServerThread
from django.test.utils import modify_settings

from channels.routing import get_default_application
from channels.staticfiles import StaticFilesWrapper
from daphne.endpoints import build_endpoint_description_strings
from daphne.server import Server


class DaphneLiveServerThread(LiveServerThread):
    """LiveServerThread subclass that runs Daphne, as a singleton"""
    singleton = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.application = self.get_application()
        DaphneLiveServerThread.singleton = self

    def run(self):
        """
        Set up the live server and databases, and then loop over handling
        HTTP requests.
        """
        if self.connections_override:
            # Override this thread's database connections with the ones
            # provided by the main thread.
            for alias, conn in self.connections_override.items():
                connections[alias] = conn
        try:
            self.daphne = self._create_server()
            self.daphne.run()
        except Exception as e:
            self.error = e
            self.is_ready.set()
        finally:
            connections.close_all()

    def _create_server(self):
        application = self.application
        if self.static_handler:
            application = self.static_handler(application)

        endpoints = build_endpoint_description_strings(host=self.host, port=self.port)

        def ready():
            if self.port == 0:
                self.port = self.daphne.listening_addresses[0][1]
            self.is_ready.set()

        return Server(
            application=application,
            endpoints=endpoints,
            signal_handlers=False,
            root_path=getattr(settings, "FORCE_SCRIPT_NAME", "") or "",
            ready_callable=ready,
        )

    def get_application(self):
        application = get_default_application()
        if self.static_handler:
            application = self.static_handler(application)

        return application

    def pause(self):
        """Pause daphne"""
        self.application = None
        self.daphne.clear()

    def unpause(self, connections_override):
        """Unpause daphne, with new connections_override"""
        # TODO: override these connections in the actual thread. This function runs in the main thread.
        for alias, conn in connections_override.items():
            connections[alias] = conn
        self.application = self.get_application()
        self.daphne.set_application(self.application)

    def terminate(self):
        if hasattr(self, 'daphne'):
           # Stop the ASGI server
           self.daphne.stop()
        self.join()


class ChannelsLiveServerTestCase(LiveServerTestCase):
    """
    Drop-in replacement for Django's LiveServerTestCase.
    """
    server_thread_class = DaphneLiveServerThread
    static_handler = StaticFilesWrapper

    @classmethod
    def setUpClass(cls):
        """
        This is code is taken from Django's LiveServerTestCase, but uses the
        server thread as a singleton.
        """
        connections_override = {}
        for conn in connections.all():
            # If using in-memory sqlite databases, pass the connections to
            # the server thread.
            if conn.vendor == 'sqlite' and conn.is_in_memory_db():
                # Explicitly enable thread-shareability for this connection
                conn.allow_thread_sharing = True
                connections_override[conn.alias] = conn

        cls._live_server_modified_settings = modify_settings(
            ALLOWED_HOSTS={'append': cls.host},
        )
        cls._live_server_modified_settings.enable()
        if hasattr(cls.server_thread_class, 'singleton') and cls.server_thread_class.singleton:
            cls.server_thread = cls.server_thread_class.singleton
            cls.server_thread.unpause(connections_override)
        else:
            cls.server_thread = cls._create_server_thread(connections_override)
            cls.server_thread.daemon = True
            cls.server_thread.start()

            # Wait for the live server to be ready
            cls.server_thread.is_ready.wait()
            if cls.server_thread.error:
                # Clean up behind ourselves, since tearDownClass won't get called in
                # case of errors.
                cls._tearDownClassInternal()
                raise cls.server_thread.error

    @classmethod
    def _tearDownClassInternal(cls):
        """
        This is code is taken from Django's LiveServerTestCase, but pauses the
        daphne server, instead of killing it.
        """
        # There may not be a 'server_thread' attribute if setUpClass() for some
        # reasons has raised an exception.
        if hasattr(cls, 'server_thread'):
            # Pause the live server's thread
            cls.server_thread.pause()

        # Restore sqlite in-memory database connections' non-shareability
        for conn in connections.all():
            if conn.vendor == 'sqlite' and conn.is_in_memory_db():
                conn.allow_thread_sharing = False
