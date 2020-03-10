import os
import sys
import importlib
sys.path.insert(0,os.getcwd() + '/../editor')

class PLCModelRunner:
    def __init__(self, model_name, model_args=None, wait_signal=False):
        self._ProgramList = None
        self._VariablesList = None
        self._DbgVariablesList = None
        self._IECPathToIdx = None
        self.PLC = None # type: PLCObject
        self.config = None
        if model_args is None:
            model_args = ''
        # Get OpenPLC path
        sys.path.insert(0, os.path.abspath(os.getcwd() + '/../editor/runtime/'))
        # Import PCLObject
        self.PLCObject = importlib.import_module('PLCObject')
        self.DebugTypesSize = importlib.import_module('typemapping').DebugTypesSize
        # Load PLC library
        library_name = os.getcwd().split(self.pd())[-1] + self._getLibraryPrefix()
        PLC = self.PLCObject.PLCObject(os.getcwd(), [], [], None, None)
        PLC._LoadPLC(self._getBuildPath() + self.pd() + library_name)
        PLC.PLCStatus = "Stopped"
        # Library load successfully


        self.GetIECProgramsAndVariables()

        config = {}
        config["Platform"] = self.GetDefaultTargetName()
        config["ProjectPath"] = os.getcwd()
        config["PLCVars"] = self._DbgVariablesList
        config['Sample_rate'] = 1.0
        config['Enable_model'] = 'true'
        config['log'] = self.log
        # config['StopPLC'] = exit
        config['standalone'] = True
        config['Script_name'] = model_name
        config['Script_args'] = model_args
        config['port'] = '4000'
        config['address'] = '127.0.0.1'
        config['wait_server'] = int(wait_signal)
        # Generate config class

        class config_class:
            def __init__(self):
                for param in config:
                    setattr(self, param, config[param])

        # Create config object
        self.PLC = PLC
        self.config = config
        self.config_class = config_class()
        self.model = importlib.import_module(model_name.split('.py')[0])

    def log(self, msg):
        print(msg)

    def pd(self):
            if 'win32' in sys.platform:
                return '\\'
            else:
                return '/'

    def GetDefaultTargetName(self):
        if 'win32' in sys.platform:
            return "Win32"
        else:
            return "Linux"

    def _getBuildPath(self):
        return os.getcwd() + self.pd() + 'build'

    def _getLibraryPrefix(self):
        os = sys.platform
        if 'win32' in os:
            return '.dll'
        else:
            return '.so'

    def Start(self, config = None):
        if config is None:
            config = self.config
        self.PLC.StartPLC(config,standalone=True)

    def GetIECProgramsAndVariables(self):

        """
        Parse CSV-like file  VARIABLES.csv resulting from IEC2C compiler.
        Each section is marked with a line staring with '//'
        list of all variables used in various POUs
        """
        if self._ProgramList is None or self._VariablesList is None:
            try:
                csvfile = os.path.join(self._getBuildPath(), "VARIABLES.csv")
                # describes CSV columns
                ProgramsListAttributeName = ["num", "C_path", "type"]
                VariablesListAttributeName = [
                    "num", "vartype", "IEC_path", "C_path", "type"]
                self._ProgramList = []
                self._VariablesList = []
                self._DbgVariablesList = []
                self._IECPathToIdx = {}

                # Separate sections
                ListGroup = []
                for line in open(csvfile, 'r').readlines():
                    strippedline = line.strip()
                    if strippedline.startswith("//"):
                        # Start new section
                        ListGroup.append([])
                    elif len(strippedline) > 0 and len(ListGroup) > 0:
                        # append to this section
                        ListGroup[-1].append(strippedline)

                # first section contains programs
                for line in ListGroup[0]:
                    # Split and Maps each field to dictionnary entries
                    attrs = dict(
                        zip(ProgramsListAttributeName, line.strip().split(';')))
                    # Truncate "C_path" to remove conf an resources names
                    attrs["C_path"] = '__'.join(
                        attrs["C_path"].split(".", 2)[1:])
                    # Push this dictionnary into result.
                    self._ProgramList.append(attrs)

                # second section contains all variables
                config_FBs = {}
                Idx = 0
                for line in ListGroup[1]:
                    # Split and Maps each field to dictionnary entries
                    attrs = dict(
                        zip(VariablesListAttributeName, line.strip().split(';')))
                    # Truncate "C_path" to remove conf an resources names
                    parts = attrs["C_path"].split(".", 2)
                    if len(parts) > 2:
                        config_FB = config_FBs.get(tuple(parts[:2]))
                        if config_FB:
                            parts = [config_FB] + parts[2:]
                            attrs["C_path"] = '.'.join(parts)
                        else:
                            attrs["C_path"] = '__'.join(parts[1:])
                    else:
                        attrs["C_path"] = '__'.join(parts)
                        if attrs["vartype"] == "FB":
                            config_FBs[tuple(parts)] = attrs["C_path"]
                    if attrs["vartype"] != "FB" and attrs["type"] in self.DebugTypesSize:
                        # Push this dictionnary into result.
                        self._DbgVariablesList.append(attrs)
                        # Fill in IEC<->C translation dicts
                        IEC_path = attrs["IEC_path"]
                        self._IECPathToIdx[IEC_path] = (Idx, attrs["type"])
                        # Ignores numbers given in CSV file
                        # Idx=int(attrs["num"])
                        # Count variables only, ignore FBs
                        attrs["name"] = attrs["C_path"].split(".")[-1] # Get the name of the variable
                        Idx += 1
                    self._VariablesList.append(attrs)

                # third section contains ticktime
                if len(ListGroup) > 2:
                    self._Ticktime = int(ListGroup[2][0])

            except Exception as e:
                print("Cannot open/parse VARIABLES.csv!\n")
                return False

        return True






