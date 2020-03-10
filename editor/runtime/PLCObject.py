#!/usr/bin/env python
# -*- coding: utf-8 -*-

# This file is part of Beremiz runtime.
#
# Copyright (C) 2007: Edouard TISSERANT and Laurent BESSARD
#
# See COPYING.Runtime file for copyrights details.
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.

# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA


from __future__ import absolute_import
from threading import Thread, Lock, Semaphore, Event, Timer
import ctypes
import os
import sys
import traceback
import shutil
from time import time
import hashlib
from tempfile import mkstemp
from functools import wraps, partial
from six.moves import xrange
from past.builtins import execfile
import _ctypes

from runtime.typemapping import TypeTranslator
from runtime.loglevels import LogLevelsDefault, LogLevelsCount
from runtime.Stunnel import getPSKID
from runtime import PlcStatus
from runtime import MainWorker
import importlib
import imp
from flask import Flask
from flask_socketio import SocketIO
from time import sleep




if os.name in ("nt", "ce"):
    dlopen = _ctypes.LoadLibrary
    dlclose = _ctypes.FreeLibrary
elif os.name == "posix":
    dlopen = _ctypes.dlopen
    dlclose = _ctypes.dlclose


def get_last_traceback(tb):
    while tb.tb_next:
        tb = tb.tb_next
    return tb


lib_ext = {
    "linux2": ".so",
    "win32":  ".dll",
}.get(sys.platform, "")


def PLCprint(message):
    sys.stdout.write("PLCobject : "+message+"\n")
    sys.stdout.flush()


def RunInMain(func):

    if func.__code__.co_argcount > 2 and \
            'standalone' in list(func.__code__.co_varnames):
        # Immediately returns for standalone applications
        return func
    @wraps(func)
    def func_wrapper(*args, **kwargs):
        return MainWorker.call(func, *args, **kwargs)
    return func_wrapper


class PLCObject(object):
    def __init__(self, WorkingDir, argv, statuschange, evaluator, pyruntimevars):
        # Initialize base class

        self.workingdir = WorkingDir  # must exits already
        self.tmpdir = os.path.join(WorkingDir, 'tmp')
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir)
        os.mkdir(self.tmpdir)
        # FIXME : is argv of any use nowadays ?
        self.argv = [WorkingDir] + argv  # force argv[0] to be "path" to exec...
        self.statuschange = statuschange
        self.evaluator = evaluator
        self.pyruntimevars = pyruntimevars
        self.PLCStatus = PlcStatus.Empty
        self.PLClibraryHandle = None
        self.PLClibraryLock = Lock()
        # Creates fake C funcs proxies
        self._InitPLCStubCalls()
        self._loading_error = None
        self.python_runtime_vars = None
        self.TraceThread = None
        self.TraceLock = Lock()
        self.Traces = []
        self.DebugToken = 0
        self.model_thread = None
        self.model = None
        # print("WorkingDir: "+ str(WorkingDir))
        # print("argv: "+ str(argv))
        # print("statuschange: "+ str(statuschange))
        # print("evaluator: "+ str(evaluator))
        # print("pyruntimevars: "+ str(pyruntimevars))
        self._init_blobs()

    # First task of worker -> no @RunInMain
    def AutoLoad(self, autostart):
        # Get the last transfered PLC
        try:
            self.CurrentPLCFilename = open(
                self._GetMD5FileName(),
                "r").read().strip() + lib_ext
            if self.LoadPLC():
                self.PLCStatus = PlcStatus.Stopped
                if autostart:
                    self.StartPLC()
                    return
        except Exception:
            self.PLCStatus = PlcStatus.Empty
            self.CurrentPLCFilename = None

        self.StatusChange()

    def StatusChange(self):
        if self.statuschange is not None:
            for callee in self.statuschange:
                callee(self.PLCStatus)

    @RunInMain
    def LogMessage(self, *args):
        if len(args) == 2:
            level, msg = args
        else:
            level = LogLevelsDefault
            msg, = args
        PLCprint(msg)
        if self._LogMessage is not None:
            return self._LogMessage(level, msg, len(msg))
        return None

    @RunInMain
    def ResetLogCount(self):
        if self._ResetLogCount is not None:
            self._ResetLogCount()

    # used internaly
    def GetLogCount(self, level):
        if self._GetLogCount is not None:
            return int(self._GetLogCount(level))
        elif self._loading_error is not None and level == 0:
            return 1

    @RunInMain
    def GetLogMessage(self, level, msgid):
        tick = ctypes.c_uint32()
        tv_sec = ctypes.c_uint32()
        tv_nsec = ctypes.c_uint32()
        if self._GetLogMessage is not None:
            maxsz = len(self._log_read_buffer)-1
            sz = self._GetLogMessage(level, msgid,
                                     self._log_read_buffer, maxsz,
                                     ctypes.byref(tick),
                                     ctypes.byref(tv_sec),
                                     ctypes.byref(tv_nsec))
            if sz and sz <= maxsz:
                self._log_read_buffer[sz] = '\x00'
                return self._log_read_buffer.value, tick.value, tv_sec.value, tv_nsec.value
        elif self._loading_error is not None and level == 0:
            return self._loading_error, 0, 0, 0
        return None

    def _GetMD5FileName(self):
        return os.path.join(self.workingdir, "lasttransferedPLC.md5")

    def _GetLibFileName(self):
        return os.path.join(self.workingdir, self.CurrentPLCFilename)

    def _LoadPLC(self, library_name = None):
        """
        Load PLC library
        Declare all functions, arguments and return values
        """
        if library_name == None:
            md5 = open(self._GetMD5FileName(), "r").read()
        self.PLClibraryLock.acquire()
        try:
            if library_name == None:
                self._PLClibraryHandle = dlopen(self._GetLibFileName())
            else:
                md5 = [0]
                self.workingdir = os.getcwd()
                self.CurrentPLCFilename = library_name.split('\\')[-1]
                # sys.path.insert(0,os.getcwd() + '/build/')
                self._PLClibraryHandle = dlopen(library_name)
            self.PLClibraryHandle = ctypes.CDLL(self.CurrentPLCFilename, handle=self._PLClibraryHandle)

            self.PLC_ID = ctypes.c_char_p.in_dll(self.PLClibraryHandle, "PLC_ID")
            if len(md5) == 32:
                self.PLC_ID.value = md5

            self._startPLC = self.PLClibraryHandle.startPLC
            self._startPLC.restype = ctypes.c_int
            self._startPLC.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)]

            self._stopPLC_real = self.PLClibraryHandle.stopPLC
            self._stopPLC_real.restype = None

            self._PythonIterator = getattr(self.PLClibraryHandle, "PythonIterator", None)
            if self._PythonIterator is not None:
                # print("self._PythonIterator is not None")
                self._PythonIterator.restype = ctypes.c_char_p
                self._PythonIterator.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]

                self._stopPLC = self._stopPLC_real
            else:
                # If python confnode is not enabled, we reuse _PythonIterator
                # as a call that block pythonthread until StopPLC
                self.PlcStopping = Event()

                def PythonIterator(res, blkid):
                    self.PlcStopping.clear()
                    self.PlcStopping.wait()
                    return None
                self._PythonIterator = PythonIterator

                def __StopPLC():
                    self._stopPLC_real()
                    self.PlcStopping.set()
                self._stopPLC = __StopPLC

            self._ResetDebugVariables = self.PLClibraryHandle.ResetDebugVariables
            self._ResetDebugVariables.restype = None

            self._RegisterDebugVariable = self.PLClibraryHandle.RegisterDebugVariable
            self._RegisterDebugVariable.restype = None
            self._RegisterDebugVariable.argtypes = [ctypes.c_int, ctypes.c_void_p]

            self._FreeDebugData = self.PLClibraryHandle.FreeDebugData
            self._FreeDebugData.restype = None

            self._GetDebugData = self.PLClibraryHandle.GetDebugData
            self._GetDebugData.restype = ctypes.c_int
            self._GetDebugData.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p)]

            self._GetAllDebugData = self.PLClibraryHandle.GetAllDebugData
            self._GetAllDebugData.restype = ctypes.c_int
            self._GetAllDebugData.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p)]

            self._WaitPLC = self.PLClibraryHandle.WaitPLC
            self._WaitPLC.restype = ctypes.c_int
            self._WaitPLC.argtypes = [ctypes.c_uint32]

            self._FinishPLC = self.PLClibraryHandle.FinishPLC
            self._FinishPLC.restype = None

            self._TryWaitSimu = self.PLClibraryHandle.TryWaitSimu
            self._TryWaitSimu.restype = ctypes.c_int

            self._FinishSimu = self.PLClibraryHandle.FinishSimu
            self._FinishSimu.restype = None

            self._WaitSimu = self.PLClibraryHandle.WaitSimu
            self._WaitSimu.restype = None

            self._SetSimulationStatus = self.PLClibraryHandle.SetSimulationStatus
            self._SetSimulationStatus.restype = None
            self._SetSimulationStatus.argtypes = [ctypes.c_int]

            self._GetSimulationStatus = self.PLClibraryHandle.GetSimulationStatus
            self._GetSimulationStatus.restype = ctypes.c_int8

            self._config_init__ = self.PLClibraryHandle.config_init__
            self._WaitSimu.restype = None

            self._suspendDebug = self.PLClibraryHandle.suspendDebug
            self._suspendDebug.restype = ctypes.c_int
            self._suspendDebug.argtypes = [ctypes.c_int]

            self._resumeDebug = self.PLClibraryHandle.resumeDebug
            self._resumeDebug.restype = None

            self._ResetLogCount = self.PLClibraryHandle.ResetLogCount
            self._ResetLogCount.restype = None

            self._GetLogCount = self.PLClibraryHandle.GetLogCount
            self._GetLogCount.restype = ctypes.c_uint32
            self._GetLogCount.argtypes = [ctypes.c_uint8]

            self._LogMessage = self.PLClibraryHandle.LogMessage
            self._LogMessage.restype = ctypes.c_int
            self._LogMessage.argtypes = [ctypes.c_uint8, ctypes.c_char_p, ctypes.c_uint32]

            self._log_read_buffer = ctypes.create_string_buffer(1 << 14)  # 16K
            self._GetLogMessage = self.PLClibraryHandle.GetLogMessage
            self._GetLogMessage.restype = ctypes.c_uint32
            self._GetLogMessage.argtypes = [ctypes.c_uint8, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]

            self._loading_error = None

        except Exception:
            self._loading_error = traceback.format_exc()
            PLCprint(self._loading_error)
            return False
        finally:
            self.PLClibraryLock.release()

        return True

    @RunInMain
    def LoadPLC(self):
        res = self._LoadPLC()
        if res:
            self.PythonRuntimeInit()
        else:
            self._FreePLC()

        return res

    @RunInMain
    def UnLoadPLC(self):
        self.PythonRuntimeCleanup()
        self._FreePLC()

    def _InitPLCStubCalls(self):
        """
        create dummy C func proxies
        """
        self._startPLC = lambda x, y: None
        self._stopPLC = lambda: None
        self._ResetDebugVariables = lambda: None
        self._RegisterDebugVariable = lambda x, y: None
        self._IterDebugData = lambda x, y: None
        self._FreeDebugData = lambda: None
        self._GetDebugData = lambda: -1
        self._suspendDebug = lambda x: -1
        self._resumeDebug = lambda: None
        self._PythonIterator = lambda: ""
        self._GetLogCount = None
        self._LogMessage = None
        self._GetLogMessage = None
        self._PLClibraryHandle = None
        self.PLClibraryHandle = None

    def _FreePLC(self):
        """
        Unload PLC library.
        This is also called by __init__ to create dummy C func proxies
        """
        self.PLClibraryLock.acquire()
        try:
            # Unload library explicitely
            if getattr(self, "_PLClibraryHandle", None) is not None:
                dlclose(self._PLClibraryHandle)

            # Forget all refs to library
            self._InitPLCStubCalls()

        finally:
            self.PLClibraryLock.release()

        return False

    def PythonRuntimeCall(self, methodname):
        """
        Calls init, start, stop or cleanup method provided by
        runtime python files, loaded when new PLC uploaded
        """
        for method in self.python_runtime_vars.get("_runtime_%s" % methodname, []):
            _res, exp = self.evaluator(method)
            if exp is not None:
                self.LogMessage(0, '\n'.join(traceback.format_exception(*exp)))

    # used internaly
    def PythonRuntimeInit(self):
        MethodNames = ["init", "start", "stop", "cleanup"]
        self.python_runtime_vars = globals().copy()
        self.python_runtime_vars.update(self.pyruntimevars)
        parent = self

        class PLCSafeGlobals(object):
            def __getattr__(self, name):
                try:
                    t = parent.python_runtime_vars["_"+name+"_ctype"]
                except KeyError:
                    raise KeyError("Try to get unknown shared global variable : %s" % name)
                v = t()
                parent.python_runtime_vars["_PySafeGetPLCGlob_"+name](ctypes.byref(v))
                return parent.python_runtime_vars["_"+name+"_unpack"](v)

            def __setattr__(self, name, value):
                try:
                    t = parent.python_runtime_vars["_"+name+"_ctype"]
                except KeyError:
                    raise KeyError("Try to set unknown shared global variable : %s" % name)
                v = parent.python_runtime_vars["_"+name+"_pack"](t, value)
                parent.python_runtime_vars["_PySafeSetPLCGlob_"+name](ctypes.byref(v))

        self.python_runtime_vars.update({
            "PLCGlobals":     PLCSafeGlobals(),
            "WorkingDir":     self.workingdir,
            "PLCObject":      self,
            "PLCBinary":      self.PLClibraryHandle,
            "PLCGlobalsDesc": []})

        for methodname in MethodNames:
            self.python_runtime_vars["_runtime_%s" % methodname] = []

        try:
            filenames = os.listdir(self.workingdir)
            filenames.sort()
            for filename in filenames:
                name, ext = os.path.splitext(filename)
                if name.upper().startswith("RUNTIME") and ext.upper() == ".PY":
                    execfile(os.path.join(self.workingdir, filename), self.python_runtime_vars)
                    for methodname in MethodNames:
                        method = self.python_runtime_vars.get("_%s_%s" % (name, methodname), None)
                        if method is not None:
                            self.python_runtime_vars["_runtime_%s" % methodname].append(method)
        except Exception:
            self.LogMessage(0, traceback.format_exc())
            raise

        self.PythonRuntimeCall("init")

    # used internaly
    def PythonRuntimeCleanup(self):
        if self.python_runtime_vars is not None:
            self.PythonRuntimeCall("cleanup")

        self.python_runtime_vars = None

    def PythonThreadProc(self):
        self.StartSem.release()
        res, cmd, blkid = "None", "None", ctypes.c_void_p()
        compile_cache = {}
        # print("python_runtime_vars: " + str(self.python_runtime_vars))
        #self.LogMessage("2)")
        # print("1)")
        while True:
            # print("2)")
            cmd = self._PythonIterator(res, blkid)
            FBID = blkid.value
            if cmd is None:
                break
            try:
                self.python_runtime_vars["FBID"] = FBID
                ccmd, AST = compile_cache.get(FBID, (None, None))
                if ccmd is None or ccmd != cmd:
                    AST = compile(cmd, '<plc>', 'eval')
                    compile_cache[FBID] = (cmd, AST)
                result, exp = self.evaluator(eval, AST, self.python_runtime_vars)
                if exp is not None:
                    res = "#EXCEPTION : "+str(exp[1])
                    self.LogMessage(1, ('PyEval@0x%x(Code="%s") Exception "%s"') % (
                        FBID, cmd, '\n'.join(traceback.format_exception(*exp))))
                else:
                    res = str(result)
                self.python_runtime_vars["FBID"] = None
            except Exception as e:
                res = "#EXCEPTION : "+str(e)
                self.LogMessage(1, ('PyEval@0x%x(Code="%s") Exception "%s"') % (FBID, cmd, str(e)))

    def ThreadModelLoop(self):

        try:
            while self._GetSimulationStatus() == 1:
                self._WaitPLC(0)
                self.model.loop(self.PLC_VARS, self.config)
                self.PLC_VARS.__ticks__ += 1
                self._FinishSimu()
        except Exception as e:

            self._FinishSimu()
            self.LogMessage(0,
                            "Model error: " + str(e))
            self.StopPLC()

    def module_exists(self, module_name):
        try:
            imp.find_module(module_name)
            return True
        except ImportError:
            return False

    # This function dynamically generates a ctype class with the PLC variables
    # and makes this variables available to a simple class which only holds the
    # variable pointers
    def GeneratePLCStruct(self, vars, buffer_ptr):
        struct_fields = []
        ctypes_type = None
        # Add the correct types for the new cint struct
        for var in vars:
            type_name = var['type']
            if type_name == 'BOOL':
                ctypes_type = ctypes.c_bool
            elif type_name == 'DINT':
                ctypes_type = ctypes.c_int32
            elif type_name == 'REAL':
                ctypes_type = ctypes.c_float
            elif type_name == 'TIME':
                ctypes_type = ctypes.c_uint32
            elif type_name == 'SINT':
                ctypes_type = ctypes.c_int8

            struct_fields.append((var['name'], ctypes.POINTER(ctypes_type)))

        class PLC_DATA(ctypes.Structure):
            _fields_ = struct_fields

        # Cleanup the new cint class
        pointer_class = PLC_DATA.from_address(buffer_ptr.value)

        class new_class:
            def __init__(self):
                self.__vars__ = []
                self.__data__ = {}
                self.__ticks__ = 0
                for var in vars:
                    setattr(self,var['name'],getattr(pointer_class,var['name']).contents)
                    self.__vars__.append(var['name'])
                    self.__data__[var['name']] = getattr(pointer_class,var['name']).contents

            def __len__(self):
                return len(self.__vars__)

            def Iterate(self):
                return self.__data__

            def GetVars(self):
                return self.__vars__

            def GetData(self):
                obj = {}
                for k in self.__data__:
                    obj[k] = self.__data__[k].value
                obj['ticks'] = self.__ticks__
                return obj

            def FillData(self, obj):
                for k in self.__data__:
                    obj[k] = self.__data__[k].value
                obj['ticks'] = self.__ticks__
                return obj

        return new_class()

    def import_module_safe(self, module_name):
        sys.path.insert(0, self.ProjectPath)
        self.model = importlib.import_module(module_name)

    def SimpleLog(self,*args):
        print(args[len(args)-1])

    def ResetPLC(self):
        self.PLC_VARS.__ticks__ = 0
        self._config_init__()

    @RunInMain
    def StartPLC(self, config, standalone = False):
        if self.CurrentPLCFilename is not None and self.PLCStatus == PlcStatus.Stopped:
            for key in config:
                setattr(self, key, config[key])

            self.Enable_model = (config["Enable_model"]=='true')

            if self.Enable_model == True:
                self._SetSimulationStatus(1)

            c_argv = ctypes.c_char_p * len(self.argv)
            res = self._startPLC(len(self.argv), c_argv(*self.argv))
            if res == 0:
                self.PLCStatus = PlcStatus.Started
                self.StatusChange()
                if standalone == False:
                    self.PythonRuntimeCall("start")
                else:
                    # Change log funtion if running in standalone
                    self.LogMessage = self.SimpleLog

                self.StartSem = Semaphore(0)
                self.PythonThread = Thread(target=self.PythonThreadProc)
                self.PythonThread.start()
                self.StartSem.acquire()
                self.LogMessage("PLC started")

                if self.Enable_model == True:

                    if self.Script_name != '' or '.py' in self.Script_name:

                        try:
                            # format script arguments
                            args = [v.split('=') if v != '' else None for v in self.Script_args.split(';')]

                            for k in args:
                                if k and len(k) >= 2:
                                    config[k[0]] = k[1]
                            # Pass callback to log messages through the IDE
                            config['Sample_rate'] = float(self.Sample_rate)
                            config['log'] = self.LogMessage
                            config['StopPLC'] = self.StopPLC
                            config['SocketIO'] = SocketIO
                            config['Flask'] = Flask
                            if 'standalone' not in config:
                                config['standalone'] = False
                            config['ResetPLC'] = self.ResetPLC

                            separator = ['\\' if self.Platform == 'Win32' else '/'][0]
                            self.script_path = self.ProjectPath + separator + self.Script_name
                            self.LogMessage("PLCPlant model path: " + self.script_path)
                            module_name = self.Script_name.split('.')[0]

                            if self.module_exists(module_name) is False or self.model is None:

                                sys.path.insert(0, self.ProjectPath)
                                self.model = importlib.import_module(module_name)

                                self.LogMessage("PLCPlant model loaded")
                            else:
                                imp.reload(self.model)
                                self.LogMessage("PLCPlant model reloaded")

                            # Initialize the plc structure classes

                            tick1 = ctypes.c_uint32()
                            size1 = ctypes.c_uint32()
                            buff1 = ctypes.c_void_p()
                            self._GetAllDebugData(ctypes.byref(tick1),
                                                  ctypes.byref(size1),
                                                  ctypes.byref(buff1))

                            # PLCVars comes from ProjectController.py
                            self.PLC_VARS = self.GeneratePLCStruct(self.PLCVars, buff1)

                            # Generate useful class to facilitate access to configuration
                            class new_config:
                                def __init__(self):
                                    for c in config:
                                        setattr(self, c, config[c])

                            self.config = new_config()


                            # Execute script initialization
                            self.model.setup(self.PLC_VARS, self.config)

                            # Execute loop scheduler
                            self.model_thread = Thread(target=self.ThreadModelLoop)
                            self.model_thread.start()




                        except Exception as e:
                            self.LogMessage(0,
                                            "Problem starting Model script: " + str(e) + ". Aborting model execution")
                            self.StopPLC()
                            return False
                    else:
                        self.LogMessage(0, "Script name invalid. Aborting model execution")
                        self.StopPLC()
                        return False


            else:
                self.LogMessage(0, _("Problem starting PLC : error %d" % res))
                self.PLCStatus = PlcStatus.Broken
                self.StatusChange()

    @RunInMain
    def StopPLC(self):
        self._SetSimulationStatus(0)
        self._FinishSimu()

        if self.PLCStatus == PlcStatus.Started:
            self.LogMessage("PLC stopped")

            self._stopPLC()
            self.PythonThread.join()
            self.PLCStatus = PlcStatus.Stopped
            self.StatusChange()
            self.PythonRuntimeCall("stop")
            if self.TraceThread is not None:
                self.TraceThread.join()
                self.TraceThread = None

            if hasattr(self.model, 'WebServer'):
                if self.model.WebServer.SocketIO is not None:
                    pass
                    # self.model.WebServer.SocketIO.stop()
                    # self.model.WebServer.raise_exception()

            return True
        return False

    @RunInMain
    def GetPLCstatus(self):
        return self.PLCStatus, map(self.GetLogCount, xrange(LogLevelsCount))

    @RunInMain
    def GetPLCID(self):
        return getPSKID(partial(self.LogMessage, 0))

    def _init_blobs(self):
        self.blobs = {}
        if os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir)
        os.mkdir(self.tmpdir)

    @RunInMain
    def SeedBlob(self, seed):
        blob = (mkstemp(dir=self.tmpdir) + (hashlib.new('md5'),))
        _fobj, _path, md5sum = blob
        md5sum.update(seed)
        newBlobID = md5sum.digest()
        self.blobs[newBlobID] = blob
        return newBlobID

    @RunInMain
    def AppendChunkToBlob(self, data, blobID):
        blob = self.blobs.pop(blobID, None)

        if blob is None:
            return None

        fobj, _path, md5sum = blob
        md5sum.update(data)
        newBlobID = md5sum.digest()
        os.write(fobj, data)
        self.blobs[newBlobID] = blob
        return newBlobID

    @RunInMain
    def PurgeBlobs(self):
        for fobj, _path, _md5sum in self.blobs:
            os.close(fobj)
        self._init_blobs()

    def _BlobAsFile(self, blobID, newpath):
        blob = self.blobs.pop(blobID, None)

        if blob is None:
            raise Exception(_("Missing data to create file: {}").format(newpath))

        fobj, path, _md5sum = blob
        os.close(fobj)
        shutil.move(path, newpath)

    @RunInMain
    def NewPLC(self, lib_name, plc_object, extrafiles):
        if self.PLCStatus in [PlcStatus.Stopped, PlcStatus.Empty, PlcStatus.Broken]:
            NewFileName = lib_name + lib_ext
            extra_files_log = os.path.join(self.workingdir, "extra_files.txt")

            old_PLC_filename = os.path.join(self.workingdir, self.CurrentPLCFilename) \
                if self.CurrentPLCFilename is not None \
                else None
            new_PLC_filename = os.path.join(self.workingdir, NewFileName)

            self.UnLoadPLC()

            self.LogMessage("NewPLC (%s)" % lib_name)
            self.PLCStatus = PlcStatus.Empty

            try:
                os.remove(old_PLC_filename)
                for filename in open(extra_files_log, "rt").readlines() + [extra_files_log]:
                    try:
                        os.remove(os.path.join(self.workingdir, filename.strip()))
                    except Exception:
                        pass
            except Exception:
                pass

            try:
                # Create new PLC file
                self._BlobAsFile(plc_object, new_PLC_filename)

                # Store new PLC filename based on md5 key
                open(self._GetMD5FileName(), "w").write(lib_name)

                # Then write the files
                log = open(extra_files_log, "w")
                for fname, blobID in extrafiles:
                    fpath = os.path.join(self.workingdir, fname)
                    self._BlobAsFile(blobID, fpath)
                    log.write(fname+'\n')

                # Store new PLC filename
                self.CurrentPLCFilename = NewFileName
            except Exception:
                self.PLCStatus = PlcStatus.Broken
                self.StatusChange()
                PLCprint(traceback.format_exc())
                return False

            if self.LoadPLC():
                self.PLCStatus = PlcStatus.Stopped
            else:
                self.PLCStatus = PlcStatus.Broken
            self.StatusChange()

            return self.PLCStatus == PlcStatus.Stopped
        return False

    def MatchMD5(self, MD5):
        try:
            last_md5 = open(self._GetMD5FileName(), "r").read()
            return last_md5 == MD5
        except Exception:
            pass
        return False

    @RunInMain
    def SetTraceVariablesList(self, idxs):
        """
        Call ctype imported function to append
        these indexes to registred variables in PLC debugger
        """
        self.DebugToken += 1
        if idxs:
            # suspend but dont disable
            if self._suspendDebug(False) == 0:
                # keep a copy of requested idx
                self._ResetDebugVariables()
                for idx, iectype, force in idxs:
                    if force is not None:
                        c_type, _unpack_func, pack_func = \
                            TypeTranslator.get(iectype,
                                               (None, None, None))
                        force = ctypes.byref(pack_func(c_type, force))
                    self._RegisterDebugVariable(idx, force)
                self._TracesSwap()
                self._resumeDebug()
                return self.DebugToken
        else:
            self._suspendDebug(True)
        return None

    def _TracesSwap(self):
        self.LastSwapTrace = time()
        if self.TraceThread is None and self.PLCStatus == PlcStatus.Started:
            self.TraceThread = Thread(target=self.TraceThreadProc)
            self.TraceThread.start()
        self.TraceLock.acquire()
        Traces = self.Traces
        self.Traces = []
        self.TraceLock.release()
        return Traces

    @RunInMain
    def GetTraceVariables(self, DebugToken):
        if DebugToken is not None and DebugToken == self.DebugToken:
            return self.PLCStatus, self._TracesSwap()
        return PlcStatus.Broken, []



    def TraceThreadProc(self):
        """
        Return a list of traces, corresponding to the list of required idx
        """
        self._resumeDebug()  # Re-enable debugger
        while self.PLCStatus == PlcStatus.Started:
            tick = ctypes.c_uint32()
            size = ctypes.c_uint32()
            buff = ctypes.c_void_p()
            TraceBuffer = None
            self.PLClibraryLock.acquire()
            res = self._GetDebugData(ctypes.byref(tick),
                                     ctypes.byref(size),
                                     ctypes.byref(buff))
            if res == 0:
                if size.value:
                    TraceBuffer = ctypes.string_at(buff.value, size.value)
                self._FreeDebugData()


            self.PLClibraryLock.release()

            if res != 0:
                break

            if TraceBuffer is not None:
                self.TraceLock.acquire()
                lT = len(self.Traces)
                if lT != 0 and lT * len(self.Traces[0]) > 1024 * 1024:
                    self.Traces.pop(0)
                self.Traces.append((tick.value, TraceBuffer))
                self.TraceLock.release()

            # TraceProc stops here if Traces not polled for 3 seconds
            traces_age = time() - self.LastSwapTrace
            if traces_age > 3:
                self.TraceLock.acquire()
                self.Traces = []
                self.TraceLock.release()
                self._suspendDebug(True)  # Disable debugger
                break

        self.TraceThread = None

    def RemoteExec(self, script, *kwargs):
        try:
            exec(script, kwargs)
        except Exception:
            _e_type, e_value, e_traceback = sys.exc_info()
            line_no = traceback.tb_lineno(get_last_traceback(e_traceback))
            return (-1, "RemoteExec script failed!\n\nLine %d: %s\n\t%s" %
                    (line_no, e_value, script.splitlines()[line_no - 1]))
        return (0, kwargs.get("returnVal", None))
