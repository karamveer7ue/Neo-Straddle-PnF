"""Static files that browsers re-check on every page load.

Starlette serves static files with an ETag and Last-Modified but no
Cache-Control, so browsers guess a lifetime and may reuse a cached copy for
hours without asking. After an update that mixed new and old JavaScript
modules: a new module importing a function an old cached one did not have
stopped the page script entirely. `no-cache` keeps the browser's copy but
makes it revalidate each time, which costs a quick 304 when nothing changed.
"""

from starlette.staticfiles import StaticFiles


class RevalidatingStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response
