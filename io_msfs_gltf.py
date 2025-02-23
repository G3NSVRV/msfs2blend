# ##### BEGIN GPL LICENSE BLOCK #####
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####

bl_info = {
    "name": "MSFS glTF importer",
    "author": "bestdani",
    "version": (0, 3),
    "blender": (2, 80, 0),
    "location": "File > Import > MSFS glTF",
    "description": "Imports a glTF file with Asobo extensions from the "
                   "Microsoft Flight Simulator (2020) for texture painting",
    "warning": "",
    "doc_url": "",
    "category": "Import-Export",
}

import json
import pathlib
import struct

import bpy
import bmesh

from bpy_extras.io_utils import ImportHelper
from bpy.props import StringProperty, BoolProperty, EnumProperty
from bpy.types import Operator

from typing import Callable

STRUCT_INDEX = struct.Struct('H')
STRUCT_VEC2 = struct.Struct('ee')
STRUCT_VEC3 = struct.Struct('fff')


def sub_buffer_from_view(buffer, buffer_view) -> list:
    start = buffer_view['byteOffset']
    end = start + buffer_view['byteLength']
    return buffer[start:end]


def get_start_indices(accessor, stride) -> list:
    try:
        start = accessor['byteOffset']
    except KeyError:
        start = 0
    count = accessor['count']
    end = start + count * stride
    return [i for i in range(start, end, stride)]


def get_indices(accessor_indices, buffer_indices) -> list:
    try:
        start = accessor_indices['byteOffset']
    except KeyError:
        start = 0
    end = start + accessor_indices['count'] * STRUCT_INDEX.size
    return [
        STRUCT_INDEX.unpack(buffer_indices[i:i + STRUCT_INDEX.size])[0]
        for i in range(start, end, STRUCT_INDEX.size)
    ]


def read_primitive(gltf, buffer, selected):
    attributes = selected['attributes']

    accessor_pos = gltf['accessors'][attributes['POSITION']]
    accessor_texcoord_0 = gltf['accessors'][attributes['TEXCOORD_0']]
    accessor_texcoord_1 = gltf['accessors'][attributes['TEXCOORD_1']]
    accessor_indices = gltf['accessors'][selected['indices']]
    buffer_view_indices = gltf['bufferViews'][accessor_indices['bufferView']]
    # TODO separate buffer views and buffers per accessor since they can
    #  potentially be different
    buffer_view_data = gltf['bufferViews'][accessor_pos['bufferView']]
    buffer_indices = sub_buffer_from_view(buffer, buffer_view_indices)
    buffer_data = sub_buffer_from_view(buffer, buffer_view_data)

    indices = get_indices(accessor_indices, buffer_indices)

    pos_starts = get_start_indices(
        accessor_pos, buffer_view_data['byteStride'])
    texcoord_0_starts = get_start_indices(
        accessor_texcoord_0, buffer_view_data['byteStride'])
    texcoord_1_starts = get_start_indices(
        accessor_texcoord_1, buffer_view_data['byteStride'])

    pos_values = [
        STRUCT_VEC3.unpack(buffer_data[i:i + STRUCT_VEC3.size])
        for i in pos_starts]
    texcoord_0_values = [
        STRUCT_VEC2.unpack(buffer_data[i:i + STRUCT_VEC2.size])
        for i in texcoord_0_starts]
    texcoord_1_values = [
        STRUCT_VEC2.unpack(buffer_data[i:i + STRUCT_VEC2.size])
        for i in texcoord_1_starts]

    return indices, pos_values, texcoord_0_values, texcoord_1_values


def as_tris(indices, pos_values, texcoord_values):
    triangles_indices = [
        (indices[i], indices[i + 1], indices[i + 2])
        for i in range(0, len(indices), 3)
    ]
    pos_tris = []
    texcoord_tris = []
    for tri_idx in triangles_indices:
        i1, i2, i3 = tri_idx
        pos_tris.append((pos_values[i1], pos_values[i2], pos_values[i3]))
        texcoord_tris.append(
            (texcoord_values[i1], texcoord_values[i2], texcoord_values[i3]))
    return pos_tris, texcoord_tris


def fill_mesh_data(buffer, gltf, gltf_mesh, uv0, uv1, b_mesh, mat_mapping,
                   report):
    idx_offset = 0
    primitives = gltf_mesh['primitives']

    for prim_idx, primitive in enumerate(primitives[0:]):
        idx, pos, tc0, tc1 = read_primitive(gltf, buffer, primitive)

        for p in pos:
            # converting to blender z up world
            b_mesh.verts.new((p[0], -p[2], p[1]))
        b_mesh.verts.ensure_lookup_table()

        try:
            asobo_data = primitive['extras']['ASOBO_primitive']
        except KeyError:
            # TODO enhance error message
            report({'ERROR'}, "No Asobo sub primitive")
            continue

        try:
            mat_index = mat_mapping[primitive['material']]
        except KeyError:
            mat_index = -1

        try:
            start_index = asobo_data['StartIndex']
        except KeyError:
            start_index = 0

        try:
            start_vertex = asobo_data['BaseVertexIndex']
        except KeyError:
            start_vertex = idx_offset

        tri_count = asobo_data['PrimitiveCount']
        for tri_i in range(tri_count):
            i = start_index + tri_i * 3
            face_indices = (
                idx[i + 2],
                idx[i + 1],
                idx[i + 0],
            )
            face = b_mesh.faces.new((
                b_mesh.verts[start_vertex + face_indices[0]],
                b_mesh.verts[start_vertex + face_indices[1]],
                b_mesh.verts[start_vertex + face_indices[2]],
            ))
            face.material_index = mat_index
            for i, loop in enumerate(face.loops):
                u, v = tc0[face_indices[i]]
                loop[uv0].uv = (u, 1 - v)
                u, v = tc1[face_indices[i]]
                loop[uv1].uv = (u, 1 - v)

        idx_offset += len(pos)


def create_meshes(buffer, gltf, materials, report):
    meshes = []
    for gltf_mesh in gltf['meshes']:
        bl_mesh = bpy.data.meshes.new(gltf_mesh['name'])
        meshes.append(bl_mesh)

        mat_mapping = {}
        material_count = 0
        for primitive in gltf_mesh['primitives']:
            gltf_mat_index = primitive['material']
            material = materials[gltf_mat_index]
            mesh_mat_index = bl_mesh.materials.find(material.name)
            if mesh_mat_index > -1:
                mat_mapping[gltf_mat_index] = mesh_mat_index
            else:
                mat_mapping[gltf_mat_index] = material_count
                bl_mesh.materials.append(material)
                material_count += 1

        b_mesh = bmesh.new()
        uv0 = b_mesh.loops.layers.uv.new()
        uv1 = b_mesh.loops.layers.uv.new()

        try:
            fill_mesh_data(buffer, gltf, gltf_mesh, uv0, uv1, b_mesh,
                           mat_mapping,
                           report)
        except Exception:
            mesh_name = gltf_mesh['name']
            report({'ERROR'}, f'could not handle mesh "{mesh_name}"')
            continue

        b_mesh.to_mesh(bl_mesh)
        bl_mesh.update()
    return meshes


def create_objects(nodes, meshes):
    objects = []
    for node in nodes:
        name = node['name']
        try:
            mesh = meshes[node['mesh']]
        except KeyError:
            mesh = bpy.data.meshes.new(name)

        obj = bpy.data.objects.new(name, mesh)

        try:
            trans = node['translation']
            # converting to blender z up world
            obj.location = trans[0], -trans[2], trans[1]
        except KeyError:
            obj.location = 0, 0, 0

        try:
            scale = node['scale']
            # converting to blender z up world
            obj.scale = scale[0], scale[2], scale[1]
        except KeyError:
            obj. scale = 1, 1, 1

        obj.rotation_mode = 'QUATERNION'

        try:
            rot = node['rotation']
            # converting to blender z up world
            obj.rotation_quaternion = rot[3], rot[0], -rot[2], rot[1]
        except KeyError:
            obj.rotation_quaternion = 1, 0, 0, 0

        objects.append(obj)
    return objects


def load_gltf_file(gltf_file_name):
    gltf_file_path = pathlib.Path(gltf_file_name)

    with open(gltf_file_path, 'r') as handle:
        gltf = json.load(handle)

    assert 'buffers' in gltf and len(gltf['buffers']) == 1, "Unable to handle 0 or multiple buffers"
    bin_file_name = gltf_file_path.with_name(gltf['buffers'][0]['uri'])

    with open(bin_file_name, 'rb') as handle:
        buffer = handle.read()

    return gltf, buffer


def create_materials(gltf):
    materials = []
    for gltf_mat in gltf['materials']:
        name = gltf_mat['name']
        bl_mat = bpy.data.materials.new(name)
        materials.append(bl_mat)
    return materials


def setup_object_hierarchy(bl_objects, gltf, collection):
    scene_description = gltf['scenes'][0]
    gltf_nodes = gltf['nodes']

    def add_children(bl_parent_object, gltf_parent_node):
        try:
            gltf_children = gltf_parent_node['children']
        except KeyError:
            return

        for j in gltf_children:
            gltf_child_node = gltf_nodes[j]
            bl_child_object = bl_objects[j]
            bl_child_object.parent = bl_parent_object
            collection.objects.link(bl_child_object)
            add_children(bl_child_object, gltf_child_node)

    for i in scene_description['nodes']:
        gltf_node = gltf_nodes[i]
        bl_object = bl_objects[i]
        collection.objects.link(bl_object)
        add_children(bl_object, gltf_node)


def import_msfs_gltf(context, gltf_file: str, report: Callable):
    gltf, buffer = load_gltf_file(gltf_file)

    materials = create_materials(gltf)
    meshes = create_meshes(buffer, gltf, materials, report)
    objects = create_objects(gltf['nodes'], meshes)
    setup_object_hierarchy(objects, gltf, context.collection)

    return {'FINISHED'}


class MsfsGltfImporter(Operator, ImportHelper):
    bl_idname = "msfs_gltf.importer"
    bl_label = "Import MSFS glTF file"

    filename_ext = ".gltf"

    texture_folder_name: StringProperty(
        name="Texture name",
        description="texture folder name",
        default="TEXTURE",
    )

    filter_glob: StringProperty(
        default="*.gltf",
        options={'HIDDEN'},
        maxlen=255,
    )

    def execute(self, context):
        return import_msfs_gltf(context, self.filepath, self.report)


def menu_func_import(self, context):
    self.layout.operator(MsfsGltfImporter.bl_idname, text="MSFS glTF (.gltf)")


def register():
    bpy.utils.register_class(MsfsGltfImporter)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.utils.unregister_class(MsfsGltfImporter)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)


if __name__ == "__main__":
    register()
