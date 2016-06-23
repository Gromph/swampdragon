from django import template
from django.conf import settings
from django.utils.safestring import mark_safe
<<<<<<< HEAD
=======
from django.utils.html import format_html
>>>>>>> master

register = template.Library()


@register.simple_tag()
def swampdragon_settings():
    root_url = getattr(settings, 'DRAGON_URL') or 'http://localhost:9999/'
    if not root_url.endswith('/'):
        root_url += '/'
<<<<<<< HEAD
    return mark_safe('<script type="text/javascript" src="{}settings.js"></script>'.format(root_url))
=======
    return format_html('<script type="text/javascript" src="{}settings.js"></script>',
                       mark_safe(root_url))
>>>>>>> master
