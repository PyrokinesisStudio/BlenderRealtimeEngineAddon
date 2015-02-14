if "bpy" in locals():
    import imp
    # imp.reload(renderer)
    # imp.reload(converter)
else:
    import bpy

import os
import socket
import struct
import subprocess
import sys

import bpy
import mathutils

from OpenGL.GL import *

from . import socket_api


def data_to_dict(data, rstr=""):
    basic_types = (str, float, int, bool)
    bpy_collection_type = type(bpy.data.actions)
    attrs = {}
    ignore = ('bl_rna', 'rna_type')

    if isinstance(data, bpy.types.Mesh):
        data.calc_normals_split()
        data.calc_tessface()

    for attr in [i for i in dir(data) if not i.startswith('__') and i not in ignore]:
        try:
            attr_data = getattr(data, attr)
        except AttributeError:
            print("Couldn't find attribute:", data, attr)
            continue

        if isinstance(attr_data, basic_types):
            attrs[attr] = getattr(data, attr)
        elif isinstance(attr_data, bpy_collection_type):
            attrs[attr] = [data_to_dict(i) for i in attr_data]
        elif isinstance(attr_data, (mathutils.Vector, mathutils.Color)):
            attrs[attr] = [i for i in attr_data]
        elif isinstance(attr_data, mathutils.Matrix):
            attrs[attr] = [i[:] for i in attr_data]
        elif not callable(attr_data):
            try:
                rstr += '.' + attr
                attrs[attr] = data_to_dict(attr_data, rstr)
            except AttributeError:
                pass
    return attrs


DEFAULT_WATCHLIST = [
    "actions",
    "armatures",
    "cameras",
    "images",
    "lamps",
    "materials",
    "meshes",
    "objects",
    "scenes",
    "sounds",
    "speakers",
    "textures",
    "worlds",
]


class _SocketFunc:
    def __init__(self, _socket, method_id, data_id):
        self.method_id = method_id
        self.data_id = data_id
        self.socket = _socket

    def __call__(self, data_set):
        if not self.socket:
            return

        for data in data_set:
            socket_api.send_message(self.socket, self.method_id, self.data_id, data_to_dict(data))


class _BaseFunc:
    def __call__(self, data_set):
        pass


def get_collection_name(collection):
    class_name = collection.rna_type.__class__.__name__
    clean_name = class_name.replace("BlendData", "").lower()
    return clean_name


class RealTimeEngine():
    bl_idname = 'RTE_FRAMEWORK'
    bl_label = "Real Time Engine Framework"

    def __init__(self, program=[], watch_list=DEFAULT_WATCHLIST):
        self.data_socket = None
        if program:
            # Setup socket for client engine
            self.client_process = subprocess.Popen(program)
            listen_sock = socket.socket()
            listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listen_sock.bind(("127.0.0.1", 4242))
            listen_sock.listen(1)
            self._use_sockets = True

            # Get data socket from connected client engine
            listen_sock.settimeout(3)
            try:
                self.data_socket = listen_sock.accept()[0]
                self.data_socket.setblocking(False)
                print("Socket connection established to engine!")
            except socket.timeout:
                print("Failed to establish socket connection to engine.")

        self._watch_list = [getattr(bpy.data, i) for i in watch_list]

        self._tracking_sets = {}
        for collection in self._watch_list:
            collection_name = get_collection_name(collection)
            self._tracking_sets[collection_name] = set()

        # Display image
        self.width = 1
        self.height = 1
        self.display = (ctypes.c_ubyte * 3)(0, 0, 0)

        self._old_vmat = None
        self._old_pmat = None
        self._old_viewport = None

        # Setup update functions
        for name in watch_list:
            if self._use_sockets:
                data_id = socket_api.DataIDs[name]
                add_func = _SocketFunc(self.data_socket, socket_api.MethodIDs.add, data_id)
                update_func = _SocketFunc(self.data_socket, socket_api.MethodIDs.update, data_id)
                remove_func = _SocketFunc(self.data_socket, socket_api.MethodIDs.remove, data_id)
            else:
                add_func = _BaseFunc()
                update_func = _BaseFunc()
                remove_func = _BaseFunc()

            setattr(self, "add_" + name, add_func)
            setattr(self, "update_" + name, update_func)
            setattr(self, "remove_" + name, remove_func)

        def main_loop(scene):
            try:
                self.scene_callback()
            except ReferenceError:
                bpy.app.handlers.scene_update_post.remove(main_loop)

        bpy.app.handlers.scene_update_post.append(main_loop)

        self.tex = glGenTextures(1)

    def view_update(self, context):
        """ Called when the scene is changed """
        for collection in self._watch_list:
            collection_name = get_collection_name(collection)
            collection_set = set(collection)
            tracking_set = self._tracking_sets[collection_name]

            # Check for new items
            add_method = getattr(self, "add_"+collection_name)
            add_set = collection_set - tracking_set
            add_method(add_set)
            tracking_set |= add_set

            # Check for removed items
            remove_method = getattr(self, "remove_"+collection_name)
            remove_set = tracking_set - collection_set
            remove_method(remove_set)
            tracking_set -= remove_set

            # Check for updates
            update_method = getattr(self, "update_"+collection_name)
            update_set = [item for item in collection if item.is_updated]
            update_method(update_set)

    def view_draw(self, context):
        """ Called when viewport settings change """
        region = context.region
        view = context.region_data

        vmat = view.view_matrix.copy()
        vmat_inv = vmat.inverted()
        pmat = view.perspective_matrix * vmat_inv

        viewport = [region.x, region.y, region.width, region.height]

        self.update_view(vmat, pmat, viewport)

        glGetError()
        glPushAttrib(GL_ALL_ATTRIB_BITS)

        glDisable(GL_DEPTH_TEST)
        glDisable(GL_CULL_FACE)
        glEnable(GL_TEXTURE_2D)

        glClearColor(0, 0, 1, 1)
        glClear(GL_COLOR_BUFFER_BIT)

        glMatrixMode(GL_MODELVIEW)
        glPushMatrix()
        glLoadIdentity()
        glMatrixMode(GL_PROJECTION)
        glPushMatrix()
        glLoadIdentity()

        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, self.tex)
        glTexImage2D(GL_TEXTURE_2D, 0, 3, self.width, self.height, 0, GL_RGB, GL_UNSIGNED_BYTE, self.display)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)

        glBegin(GL_QUADS)
        glColor3f(1.0, 1.0, 1.0)
        glTexCoord2f(0.0, 0.0)
        glVertex3i(-1, -1, 0)
        glTexCoord2f(1.0, 0.0)
        glVertex3i(1, -1, 0)
        glTexCoord2f(1.0, 1.0)
        glVertex3i(1, 1, 0)
        glTexCoord2f(0.0, 1.0)
        glVertex3i(-1, 1, 0)
        glEnd()

        glPopMatrix()
        glMatrixMode(GL_MODELVIEW)
        glPopMatrix()

        glPopAttrib()

    def update_view(self, view_matrix, projection_matrix, viewport):
        if not self.data_socket:
            return

        def togl(matrix):
            return [i for col in matrix.col for i in col]

        if view_matrix != self._old_vmat:
            self._old_vmat = view_matrix
            data = {"data": togl(view_matrix)}
            socket_api.send_message(self.data_socket,
                                    socket_api.MethodIDs.update,
                                    socket_api.DataIDs.view,
                                    data)

        if projection_matrix != self._old_pmat:
            self._old_pmat = projection_matrix
            data = {"data": togl(projection_matrix)}
            socket_api.send_message(self.data_socket,
                                    socket_api.MethodIDs.update,
                                    socket_api.DataIDs.projection,
                                    data)

        if viewport != self._old_viewport:
            self._old_viewport = viewport
            data = {"width": viewport[2], "height": viewport[3]}
            socket_api.send_message(self.data_socket,
                                    socket_api.MethodIDs.update,
                                    socket_api.DataIDs.viewport,
                                    data)

    def scene_callback(self):
        if not self.data_socket:
            return

        try:
            self.data_socket.setblocking(False)
            self.width, self.height = struct.unpack("HH", self.data_socket.recv(4))
            data_size = self.width * self.height * 3
            self.display = (ctypes.c_ubyte * (self.width * self.height * 3))()
            self.data_socket.setblocking(True)
            self.data_socket.settimeout(1)
            remaining = data_size
            offset = 0
            while remaining > 0:
                chunk = self.data_socket.recv(min(2**23, remaining))
                rcv_size = len(chunk)
                ctypes.memmove(ctypes.byref(self.display, offset), chunk, rcv_size)
                remaining -= rcv_size
                offset += rcv_size
            self.tag_redraw()
        except struct.error:
            pass
            print("Malformed message received.")
        except BlockingIOError:
            pass

