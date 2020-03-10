#!/usr/bin/python

import random
from time import time
from plc_runner import PLCModelRunner

class Stage1_model:
    LIT101_INIT = 500.0
    LIT101 = LIT101_INIT
    Sample_rate = 60.0
    RandomNoise = 2.5

    # Process receives pump input and output water level
    def process(self, MV101, P101):
        if MV101 == 1 and P101 == 1:
            self.LIT101 += 1.11857 / self.Sample_rate
        elif MV101 == 1 and P101 == 0:
            self.LIT101 += 28.5234 / self.Sample_rate
        elif MV101 == 0 and P101 == 1:
            self.LIT101 -= 27.40 / self.Sample_rate
        elif MV101 == 0 and P101 == 0:
            # Water level is the same
            pass
        return self.LIT101 + \
               random.uniform(-self.RandomNoise, self.RandomNoise)

    def reset(self):
        self.LIT101 = self.LIT101_INIT


# Create the stage1 model
Stage1 = Stage1_model()

# Setup is called before the PLC program starts
def setup(PLC, args):

    if args.standalone == False:
        # Update sample rate settings from OpenPLC IDE
        Stage1.Sample_rate = args.Sample_rate
    else:
        # Initialize sample rate for standalone model
        Stage1.Sample_rate = 60.0
    # Sets the Initial water level on PLC.
    PLC.LIT101.value = Stage1.LIT101

    # Print model info
    args.log("[Model] Initial water level: " + str(Stage1.LIT101))
    args.log("[Model] Sample Rate: " + str(Stage1.Sample_rate))
    args.log("[Model] Registered Vars: " + str(len(PLC)))
    args.log('[Model] HIGH level: ' + str(PLC.HIGH_LIMIT.value) +
             ', LOW level: ' + str(PLC.LOW_LIMIT.value))


# model is called each PLC scan time (configured on IDE)
# Receive all variables from PLC program and return the altered variables
def loop(PLC, args):
    # Read PLC outputs
    MV101 = PLC.MV101.value
    P101 =  PLC.P101.value
    # Execute process model (stage 1)
    LIT101 = Stage1.process(MV101, P101)
    # Update PLC input
    PLC.LIT101.value = LIT101


if __name__ == '__main__':
    PMR = PLCModelRunner('model.py', wait_signal=False)
    PMR.Start()
