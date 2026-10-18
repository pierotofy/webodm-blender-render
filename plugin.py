from app.plugins import PluginBase, MountPoint
from .api import TaskInfo, TaskRender, TaskExport


class Plugin(PluginBase):
    def include_js_files(self):
        return ["main.js"]

    def api_mount_points(self):
        return [
            MountPoint("task/(?P<pk>[^/.]+)/info", TaskInfo.as_view()),
            MountPoint("task/(?P<pk>[^/.]+)/render", TaskRender.as_view()),
            MountPoint("task/(?P<pk>[^/.]+)/export", TaskExport.as_view()),
        ]
