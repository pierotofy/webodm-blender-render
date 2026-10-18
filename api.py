import os
import re
import json
import math
import glob
import shutil
import platform

from rest_framework import status
from rest_framework.response import Response
from app.plugins.views import TaskView
from app.plugins.worker import run_function_async

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scene.py")


def version_key(path):
    return [int(n) for n in re.findall(r"\d+", os.path.basename(os.path.dirname(path)))]


def find_blender():
    env = os.environ.get("BLENDER_PATH")
    if env and os.path.isfile(env):
        return env

    candidates = []
    system = platform.system()
    if system == "Windows":
        for root in (os.environ.get("ProgramFiles", "C:\\Program Files"),
                     os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"),
                     os.environ.get("ProgramW6432", "C:\\Program Files")):
            candidates += sorted(glob.glob(os.path.join(root, "Blender Foundation", "Blender*", "blender.exe")), key=version_key, reverse=True)
            candidates.append(os.path.join(root, "Steam", "steamapps", "common", "Blender", "blender.exe"))
    elif system == "Darwin":
        candidates.append("/Applications/Blender.app/Contents/MacOS/Blender")
        candidates.append(os.path.expanduser("~/Applications/Blender.app/Contents/MacOS/Blender"))
    else:
        candidates += ["/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender"]
        candidates += sorted(glob.glob("/opt/blender*/blender"), key=version_key, reverse=True)

    which = shutil.which("blender")
    if which:
        candidates.insert(0, which)

    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def safe_name(name):
    name = re.sub(r"[^0-9a-zA-Z-_]+", "", (name or "").replace(" ", "-").replace("/", "-"))
    return re.sub(r"-+", "-", name) or "scene"


def sun_position(ts, lat, lon):
    n = ts / 86400.0 + 2440587.5 - 2451545.0
    L = (280.460 + 0.9856474 * n) % 360
    g = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * n) % 24
    ha = math.radians(gmst * 15 + lon) - ra
    phi = math.radians(lat)
    el = math.asin(math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(ha))
    az = math.atan2(-math.sin(ha), math.tan(dec) * math.cos(phi) - math.sin(phi) * math.cos(ha))
    return math.degrees(az) % 360, math.degrees(el)


def capture_sun(task):
    path = task.assets_path("images.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            images = json.load(f)
        pts = sorted((i["utc_time"] / 1000.0, float(i["latitude"]), float(i["longitude"])) for i in images
                     if i.get("utc_time") and i.get("latitude") is not None and i.get("longitude") is not None)
    except (ValueError, TypeError, KeyError, OSError):
        return None
    if not pts:
        return None
    ts, lat, lon = pts[len(pts) // 2]
    az, el = sun_position(ts - round(lon / 15) * 3600, lat, lon)
    if el <= 0:
        return None
    return {"azimuth": az, "elevation": max(5.0, el)}


def ground_color(task):
    fallback = [0.10, 0.10, 0.08]
    if "orthophoto.tif" not in task.available_assets:
        return fallback
    try:
        import rasterio
        import numpy as np
        with rasterio.open(task.get_asset_download_path("orthophoto.tif")) as ds:
            if ds.count < 3 or ds.dtypes[0] != "uint8":
                return fallback
            arr = ds.read(out_shape=(ds.count, 64, 64)).reshape(ds.count, -1)
        mask = arr[3] > 0 if arr.shape[0] >= 4 else arr[:3].sum(axis=0) > 0
        if not mask.any():
            return fallback
        srgb = arr[:3, mask].mean(axis=1) / 255.0
        return [float(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4) for c in srgb]
    except Exception:
        return fallback


def parse_config(data, task):
    engine = data.get("engine", "CYCLES")
    if engine not in ("CYCLES", "EEVEE"):
        raise ValueError("Invalid engine")
    style = data.get("style", "realistic")
    if style not in ("photo", "realistic"):
        raise ValueError("Invalid style")
    width = max(16, min(16384, int(data.get("width", 1920))))
    height = max(16, min(16384, int(data.get("height", 1080))))
    cam = data.get("camera") or {}
    matrix = [float(v) for v in cam.get("matrix", [])]
    if len(matrix) != 16 or not all(math.isfinite(v) for v in matrix):
        raise ValueError("Invalid camera")
    realistic = style == "realistic"
    sun = capture_sun(task) if realistic and data.get("sun") else None
    ground = realistic and bool(data.get("ground"))
    return {
        "engine": engine,
        "width": width,
        "height": height,
        "style": style,
        "sky": realistic and bool(data.get("sky")),
        "sun": sun is not None,
        "sun_dir": sun,
        "effects": realistic and bool(data.get("effects")),
        "ground": ground,
        "ground_color": ground_color(task) if ground else None,
        "camera": {
            "matrix": matrix,
            "fov": max(0.1, min(179.0, float(cam.get("fov", 60)))),
            "ortho": bool(cam.get("ortho", False)),
            "ortho_height": max(1e-6, float(cam.get("ortho_height", 1))),
            "near": max(1e-4, float(cam.get("near", 0.1))),
            "far": max(1.0, float(cam.get("far", 100000))),
        }
    }


def render_scene(blender, script, config, name):
    import os
    import tempfile
    import subprocess
    from webodm import settings

    tmpdir = tempfile.mkdtemp("_blender", dir=settings.MEDIA_TMP)
    with open(script, "r", encoding="utf-8") as f:
        src = f.read().replace("CONFIG = None", "CONFIG = " + repr(config), 1)
    script_path = os.path.join(tmpdir, "scene.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(src)

    out = os.path.join(tmpdir, name + ".png")
    p = subprocess.run([blender, "-b", "--factory-startup", "--python-exit-code", "1", "-P", script_path, "--", out],
                       cwd=tmpdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode != 0 or not os.path.isfile(out):
        log = p.stdout.decode("utf-8", errors="replace").strip().splitlines()
        return {"error": "Blender render failed: " + "\n".join(log[-15:])}
    return {"file": out}


def export_scene(script, config, glb, name):
    import os
    import tempfile
    import zipfile
    from webodm import settings

    tmpdir = tempfile.mkdtemp("_blender", dir=settings.MEDIA_TMP)
    with open(script, "r", encoding="utf-8") as f:
        src = f.read().replace("CONFIG = None", "CONFIG = " + repr(config), 1)

    readme = ("Blender scene for textured_model.glb\n\n"
              "Open with:\n\n"
              "    blender --python scene.py\n\n"
              "or open scene.py in Blender's Text Editor and press Run Script.\n"
              "Press F12 to render.\n")

    out = os.path.join(tmpdir, name + ".zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        z.write(glb, "textured_model.glb")
        z.writestr("scene.py", src)
        z.writestr("README.txt", readme)
    return {"file": out}


class TaskInfo(TaskView):
    def get(self, request, pk=None):
        task = self.get_and_check_task(request, pk)
        return Response({
            "glb": "textured_model.glb" in task.available_assets,
            "blender": find_blender() is not None,
            "sun": capture_sun(task) is not None
        })


class TaskRender(TaskView):
    def post(self, request, pk=None):
        task = self.get_and_check_task(request, pk)
        if "textured_model.glb" not in task.available_assets:
            return Response({"error": "No glTF model available"})
        blender = find_blender()
        if blender is None:
            return Response({"error": "Blender not found"})
        try:
            config = parse_config(request.data, task)
        except (ValueError, TypeError) as e:
            return Response({"error": str(e)})

        config["glb"] = os.path.abspath(task.get_asset_download_path("textured_model.glb"))
        name = safe_name(task.name) + "-render"
        celery_task_id = run_function_async(render_scene, blender, SCRIPT, config, name).task_id
        return Response({"celery_task_id": celery_task_id, "filename": name + ".png"}, status=status.HTTP_200_OK)


class TaskExport(TaskView):
    def post(self, request, pk=None):
        task = self.get_and_check_task(request, pk)
        if "textured_model.glb" not in task.available_assets:
            return Response({"error": "No glTF model available"})
        try:
            config = parse_config(request.data, task)
        except (ValueError, TypeError) as e:
            return Response({"error": str(e)})

        config["glb"] = "textured_model.glb"
        glb = os.path.abspath(task.get_asset_download_path("textured_model.glb"))
        name = safe_name(task.name) + "-blender"
        celery_task_id = run_function_async(export_scene, SCRIPT, config, glb, name).task_id
        return Response({"celery_task_id": celery_task_id, "filename": name + ".zip"}, status=status.HTTP_200_OK)
