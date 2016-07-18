VERSION = (0, 4, 2, 2)


try:
    from importlib import import_module
except ImportError:
    from django.utils.importlib import import_module


def discover_routes():
    from swampdragon import route_handler
    """
    Discover, borrowed from django admin.
    Returns urls for each route handler
    """
    from django.conf import settings
    try:
        from importlib import import_module
    except ImportError:
        from django.utils.importlib import import_module

    imported_routers = []
    urls = []
    for app in settings.INSTALLED_APPS:
        try:
            target_mod = '%s.routers' % app
            if target_mod not in imported_routers:
                import_module(target_mod)
            imported_routers.append(target_mod)
        except ImportError:
            pass
    routes = route_handler.registered_handlers
    for route in routes:
        urls.append(('/' + route + '/$', routes[route]))
    return urls


def load_field_deserializers():
    from django.conf import settings
    try:
        from importlib import import_module
    except ImportError:
        from django.utils.importlib import import_module

    imported_deserializers = []
    for app in settings.INSTALLED_APPS:
        try:
            target_mod = '%s.field_deserializers' % app
            if target_mod not in imported_deserializers:
                import_module(target_mod)
            imported_deserializers.append(target_mod)
        except ImportError:
            pass


middlewares = []


def discover_middleware():
    """
    """
    from django.conf import settings

    if not hasattr(settings, 'SWAMPDRAGON_MIDDLEWARE_CLASSES'):
        return []

    for middleware in settings.MIDDLEWARE_CLASSES:
        module_name, cls_name = middleware.rsplit('.')
        module = import_module(module_name)
        middlewares.append(getattr(module, cls_name))

    return middlewares
