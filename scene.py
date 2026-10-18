import bpy
import os
import sys
import json
import math
import struct
import numpy as np
from mathutils import Matrix, Vector

CONFIG = None

HORIZON = (0.55, 0.66, 0.82, 1)
ZENITH = (0.10, 0.22, 0.52, 1)
HAZE_DISTANCE = 8000.0
SUN_SHARE = 0.3
GROUND_SIZE = 200000.0


def read_rtc_center(path):
    with open(path, "rb") as f:
        f.read(12)
        length, _ = struct.unpack("<II", f.read(8))
        doc = json.loads(f.read(length))
    center = doc.get("extensions", {}).get("CESIUM_RTC", {}).get("center", [0, 0, 0])
    return Vector((center[0], center[1], 0))


def find_glb(name):
    if os.path.isabs(name):
        return name
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    if os.path.isfile(p):
        return p
    for t in bpy.data.texts:
        if t.filepath:
            p = os.path.join(os.path.dirname(bpy.path.abspath(t.filepath)), name)
            if os.path.isfile(p):
                return p
    return name


def clear():
    for coll in (bpy.data.objects, bpy.data.meshes, bpy.data.materials, bpy.data.images,
                 bpy.data.cameras, bpy.data.lights, bpy.data.worlds):
        for d in list(coll):
            coll.remove(d)


def build_material(mat, image, interpolation, extension, color, realistic, haze):
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")

    if realistic:
        sh = nt.nodes.new("ShaderNodeBsdfDiffuse")
        sh.inputs["Roughness"].default_value = 1.0
        surface = sh.outputs["BSDF"]
    else:
        sh = nt.nodes.new("ShaderNodeEmission")
        sh.inputs["Strength"].default_value = 1.0
        surface = sh.outputs["Emission"]
    sh.inputs["Color"].default_value = color

    if image is not None:
        img = nt.nodes.new("ShaderNodeTexImage")
        img.image = image
        img.interpolation = interpolation
        img.extension = extension
        nt.links.new(img.outputs["Color"], sh.inputs["Color"])

    if haze:
        cd = nt.nodes.new("ShaderNodeCameraData")
        mul = nt.nodes.new("ShaderNodeMath")
        mul.operation = "MULTIPLY"
        mul.inputs[1].default_value = -1.0 / HAZE_DISTANCE
        ex = nt.nodes.new("ShaderNodeMath")
        ex.operation = "EXPONENT"
        inv = nt.nodes.new("ShaderNodeMath")
        inv.operation = "SUBTRACT"
        inv.inputs[0].default_value = 1.0
        em = nt.nodes.new("ShaderNodeEmission")
        em.inputs["Color"].default_value = HORIZON
        mix = nt.nodes.new("ShaderNodeMixShader")
        nt.links.new(cd.outputs["View Distance"], mul.inputs[0])
        nt.links.new(mul.outputs[0], ex.inputs[0])
        nt.links.new(ex.outputs[0], inv.inputs[1])
        nt.links.new(inv.outputs[0], mix.inputs["Fac"])
        nt.links.new(surface, mix.inputs[1])
        nt.links.new(em.outputs["Emission"], mix.inputs[2])
        surface = mix.outputs["Shader"]

    nt.links.new(surface, out.inputs["Surface"])


def convert_material(mat, realistic, haze):
    image, interpolation, extension, color = None, "Linear", "REPEAT", (1, 1, 1, 1)
    if mat.use_nodes:
        nt = mat.node_tree
        tex = next((n for n in nt.nodes if n.type == "TEX_IMAGE" and n.image is not None), None)
        if tex is not None:
            image, interpolation, extension = tex.image, tex.interpolation, tex.extension
        bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is not None:
            color = tuple(bsdf.inputs["Base Color"].default_value)
    build_material(mat, image, interpolation, extension, color, realistic, haze)


def build_world(scene, realistic, sky, light_level):
    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    nt = world.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputWorld")
    light = nt.nodes.new("ShaderNodeBackground")
    light.inputs["Color"].default_value = (1, 1, 1, 1)
    light.inputs["Strength"].default_value = light_level if realistic else 0.0

    if realistic and sky:
        tc = nt.nodes.new("ShaderNodeTexCoord")
        sep = nt.nodes.new("ShaderNodeSeparateXYZ")
        ramp = nt.nodes.new("ShaderNodeValToRGB")
        ramp.color_ramp.interpolation = "EASE"
        ramp.color_ramp.elements[0].position = 0.0
        ramp.color_ramp.elements[0].color = HORIZON
        ramp.color_ramp.elements[1].position = 0.6
        ramp.color_ramp.elements[1].color = ZENITH
        bg = nt.nodes.new("ShaderNodeBackground")
        lp = nt.nodes.new("ShaderNodeLightPath")
        mix = nt.nodes.new("ShaderNodeMixShader")
        nt.links.new(tc.outputs["Generated"], sep.inputs[0])
        nt.links.new(sep.outputs["Z"], ramp.inputs["Fac"])
        nt.links.new(ramp.outputs["Color"], bg.inputs["Color"])
        nt.links.new(lp.outputs["Is Camera Ray"], mix.inputs["Fac"])
        nt.links.new(light.outputs["Background"], mix.inputs[1])
        nt.links.new(bg.outputs["Background"], mix.inputs[2])
        nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
        scene.render.film_transparent = False
    else:
        nt.links.new(light.outputs["Background"], out.inputs["Surface"])
        scene.render.film_transparent = True

    scene.world = world


def enable_gpu():
    addon = bpy.context.preferences.addons.get("cycles")
    if addon is None:
        return None
    prefs = addon.preferences
    for kind in ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"):
        try:
            prefs.compute_device_type = kind
        except TypeError:
            continue
        prefs.get_devices()
        if any(d.type == kind for d in prefs.devices):
            for d in prefs.devices:
                d.use = d.type == kind
            return kind
    prefs.compute_device_type = "NONE"
    return None


def set_engine(scene, engine, realistic, size):
    if engine == "EEVEE":
        try:
            scene.render.engine = "BLENDER_EEVEE_NEXT"
        except TypeError:
            scene.render.engine = "BLENDER_EEVEE"
        scene.eevee.taa_render_samples = 64
        if realistic:
            if hasattr(scene.eevee, "use_gtao"):
                scene.eevee.use_gtao = True
                scene.eevee.gtao_distance = max(0.2, size * 0.02)
            if hasattr(scene.eevee, "use_soft_shadows"):
                scene.eevee.use_soft_shadows = True
    else:
        scene.render.engine = "CYCLES"
        gpu = enable_gpu()
        scene.cycles.device = "GPU" if gpu else "CPU"
        print("Cycles device:", gpu or "CPU")
        if realistic:
            scene.cycles.samples = 128
            scene.cycles.use_denoising = True
            scene.cycles.max_bounces = 4
        else:
            scene.cycles.samples = 64
            scene.cycles.use_denoising = False
            scene.cycles.max_bounces = 0


def mesh_stats(objects):
    lo = np.full(3, np.inf)
    hi = np.full(3, -np.inf)
    zs = []
    for o in objects:
        if o.type != "MESH" or len(o.data.vertices) == 0:
            continue
        co = np.empty(len(o.data.vertices) * 3, dtype=np.float32)
        o.data.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        m = np.array(o.matrix_world, dtype=np.float32)
        w = co @ m[:3, :3].T + m[:3, 3]
        lo = np.minimum(lo, w.min(axis=0))
        hi = np.maximum(hi, w.max(axis=0))
        zs.append(w[:, 2])
    if not zs:
        return Vector((0, 0, 0)), Vector((0, 0, 0)), 0.0
    ground = float(np.percentile(np.concatenate(zs), 2))
    return Vector(lo), Vector(hi), ground


def add_ground(scene, lo, hi, z, color, haze):
    cx, cy, h = (lo.x + hi.x) / 2, (lo.y + hi.y) / 2, GROUND_SIZE / 2
    z -= max(0.01, (hi - lo).length * 0.001)
    me = bpy.data.meshes.new("Ground")
    me.from_pydata([(cx - h, cy - h, z), (cx + h, cy - h, z), (cx + h, cy + h, z), (cx - h, cy + h, z)], [], [(0, 1, 2, 3)])
    mat = bpy.data.materials.new("Ground")
    build_material(mat, None, "Linear", "REPEAT", tuple(color) + (1,), True, haze)
    me.materials.append(mat)
    ob = bpy.data.objects.new("Ground", me)
    scene.collection.objects.link(ob)


def add_sun(scene, sun):
    az, el = math.radians(sun["azimuth"]), math.radians(sun["elevation"])
    d = Vector((math.sin(az) * math.cos(el), math.cos(az) * math.cos(el), math.sin(el)))
    data = bpy.data.lights.new("Sun", "SUN")
    data.energy = SUN_SHARE * math.pi
    data.angle = math.radians(0.53)
    ob = bpy.data.objects.new("Sun", data)
    ob.rotation_mode = "QUATERNION"
    ob.rotation_quaternion = d.to_track_quat("Z", "Y")
    scene.collection.objects.link(ob)
    return math.sin(el)


def set_view_transform(scene, effects):
    vs = scene.view_settings
    vs.look = "None"
    vs.exposure = 0
    vs.gamma = 1
    if effects:
        for name in ("AgX", "Filmic"):
            try:
                vs.view_transform = name
            except TypeError:
                continue
            for look in (name + " - Medium High Contrast", "Medium High Contrast"):
                try:
                    vs.look = look
                    break
                except TypeError:
                    pass
            return
    vs.view_transform = "Standard"


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    out = argv[0] if argv else None

    realistic = CONFIG["style"] == "realistic"
    sky = realistic and CONFIG["sky"]
    sun = CONFIG["sun_dir"] if realistic and CONFIG["sun"] else None
    effects = realistic and CONFIG["effects"]
    ground = realistic and CONFIG["ground"]

    glb = find_glb(CONFIG["glb"])
    clear()
    scene = bpy.context.scene

    bpy.ops.import_scene.gltf(filepath=glb)
    imported = list(bpy.context.selected_objects)

    zup = Matrix.Rotation(-math.pi / 2, 4, "X")
    for o in imported:
        if o.parent is None:
            o.matrix_world = zup @ o.matrix_world
    bpy.context.view_layer.update()

    materials = set()
    for o in imported:
        for slot in getattr(o, "material_slots", []):
            if slot.material is not None:
                materials.add(slot.material)
    for mat in materials:
        convert_material(mat, realistic, sky)

    lo, hi, ground_z = mesh_stats(imported)
    size = (hi - lo).length

    if ground:
        add_ground(scene, lo, hi, ground_z, CONFIG["ground_color"], sky)

    light_level = 1.0
    if sun is not None:
        light_level = 1.0 - SUN_SHARE * max(0.0, add_sun(scene, sun))

    build_world(scene, realistic, sky, light_level)
    set_engine(scene, CONFIG["engine"], realistic, size)
    set_view_transform(scene, effects)

    scene.render.resolution_x = CONFIG["width"]
    scene.render.resolution_y = CONFIG["height"]
    scene.render.resolution_percentage = 100
    scene.render.dither_intensity = 0
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"

    c = CONFIG["camera"]
    cam_data = bpy.data.cameras.new("Camera")
    cam_data.sensor_fit = "VERTICAL"
    cam_data.clip_start = c["near"]
    cam_data.clip_end = max(c["far"], GROUND_SIZE * 1.5) if ground else c["far"]
    if c["ortho"]:
        cam_data.type = "ORTHO"
        cam_data.ortho_scale = c["ortho_height"]
    else:
        cam_data.angle_y = math.radians(c["fov"])
    cam = bpy.data.objects.new("Camera", cam_data)
    scene.collection.objects.link(cam)
    e = c["matrix"]
    m = Matrix(((e[0], e[4], e[8], e[12]),
                (e[1], e[5], e[9], e[13]),
                (e[2], e[6], e[10], e[14]),
                (0, 0, 0, 1)))
    m.translation = m.translation - read_rtc_center(glb)
    cam.matrix_world = m
    scene.camera = cam

    if effects and not c["ortho"]:
        bpy.context.view_layer.update()
        origin = m.translation
        direction = m.to_quaternion() @ Vector((0, 0, -1))
        hit, location, _, _, _, _ = scene.ray_cast(bpy.context.evaluated_depsgraph_get(), origin, direction)
        focus = (location - origin).length if hit else max(0.1, direction.dot((lo + hi) / 2 - origin))
        cam_data.dof.use_dof = True
        cam_data.dof.focus_distance = focus
        cam_data.dof.aperture_fstop = 2.8

    if out:
        scene.render.filepath = out
        bpy.ops.render.render(write_still=True)


main()
