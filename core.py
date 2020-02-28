'''
Pupil Imsoft's core module; Classes for static and dynamic imaging
'''

import os
import sys
import time
import datetime
import copy

import numpy as np

import nidaqmx

from anglepairs import saveAnglePairs, loadAnglePairs, toDegrees
from arduino_serial import ArduinoReader
from camera_client import CameraClient
from motors import Motor
from imaging_parameters import DEFAULT_DYNAMIC_PARAMETERS, ParameterEditor, getModifiedParameters
from imaging_nidaq_stimulus import get_pulse_stimulus
import macro

class Static:
    '''
    Static imaging core Class.
    '''
    pass


class Dynamic:
    '''
    Dynamic imaging procedures.
    '''

    def __init__(self, dynamic_parameters=DEFAULT_DYNAMIC_PARAMETERS):
        '''
        Sets up ArduinoReader, CameraClient/Server.
        '''

        # Angle pairs reader (rotary encoder values)
        self.reader = ArduinoReader()
        
        # Initiate camera client/server
        self.camera = CameraClient()
        if not self.camera.isServerRunning():
            self.camera.startServer()
        
        # Details about preparation (name, sex, age) are saved in this
        self.preparation = {'name': 'test', 'sex': '', 'age': ''}

        self.dynamic_parameters = dynamic_parameters
        
        self.previous_angle = None

        # Suffix to be appended in the subfolders, see set_subfolder_suffix method
        self.suffix = ''

        # Set up motors followingly
        # 0)    Horizontal + sensor
        # 1)    Vertical + sensor
        # 2)    Microscope focus (no sensor)
        self.motors = []
        for i_motor, i_sensor in zip([0,1,2],[0,1,None]):
            self.motors.append(Motor(self.reader, i_motor, i_sensor))
        

        # Macro imaging: Automatically move motors and take images
        # self macro is a list of anglepairs where to move the horizontal/vertical,
        # take image "commands" (string 'image') and other functions.
        # For more details see the tick method of this class.
        self.macro = None
        self.i_macro = 0
        self.waittime = 0

    def analog_output(self, channels, stimuli, fs, wait_trigger):
        '''

        channels    List of channel names
        stimuli     List of 1D numpy arrays
        fs          Sampling frequency of the stimuli

        '''
        with nidaqmx.Task() as task:
            for i_channel, channel in enumerate(channels):
                if type(channel) == type('string'):
                    task.ao_channels.add_ao_voltage_chan(channel)
                else:
                    
                    for subchan in channel:
                        task.ao_channels.add_ao_voltage_chan(subchan)
                        stimuli.insert(i_channel, stimuli[i_channel])
                    stimuli.pop(i_channel)
                    
            task.timing.cfg_samp_clk_timing(float(fs), samps_per_chan=len(stimuli[0]))

            
            if len(stimuli) > 1:
                stimulus = stimuli[0]
                for s in stimuli[1:]:
                    stimulus = np.vstack((stimulus, s))
            else:
                stimulus = stimuli[0]
                #stimulus = np.reshape(stimulus, (stimulus.shape[0], 1))
            #print(stimulus)

            #import matplotlib.pyplot as plt
            #for stim in stimulus:
            #    plt.plot(stim)
            #    print(len(stim))
            #plt.show()
            print(stimulus.shape)
            task.write(stimulus)#, auto_start=True)

            if wait_trigger:
                task.triggers.start_trigger.cfg_dig_edge_start_trig("/Dev1/PFI0", trigger_edge=nidaqmx.constants.Edge.RISING)

            
            task.start()
            task.wait_until_done(timeout=len(stimuli[0])*fs*1.5)


    def set_led(self, device, value, wait_trigger=False):
        '''
        Set an output channel to a specific voltage value.

        INPUT ARGUMENTS     DESCRIPTION
        device              A string (single device) or a list of strings (many devices at once)
        '''
        with nidaqmx.Task() as task:
            
            if type(device) == type('string'):
                # If there's only a single device
                task.ao_channels.add_ao_voltage_chan(device)
            else:
                # If device is actually a list of devices
                for dev in device:
                    task.ao_channels.add_ao_voltage_chan(dev)
                value = [value for i in range(len(device))]
            
            if wait_trigger:
                task.timing.cfg_samp_clk_timing(10000)
                task.triggers.start_trigger.cfg_dig_edge_start_trig("/Dev1/PFI0", trigger_edge=nidaqmx.constants.Edge.FALLING)
            task.write(value)



    def wait_for_trigger(self):
        '''
        Doesn't return until trigger signal is received.
        '''
        with nidaqmx.Task() as task:
            device = nidaqmx.system.device.Device('Dev1')
            
            task.ai_channels.add_ai_voltage_chan('Dev1/ai0' )
            
            task.timing.cfg_samp_clk_timing(10000)
            
            task.triggers.start_trigger.cfg_dig_edge_start_trig("/Dev1/PFI0", trigger_edge=nidaqmx.constants.Edge.RISING)
            task.read(number_of_samples_per_channel=1)
            #task.wait_until_done()
        


    #
    # IMAGING METHODS
    #

    def take_snap(self, save=False):
        '''
        Takes a snap image.
        
        save        Whether to save the image directly or not.
        '''
        if save:
            self.set_led(self.dynamic_parameters['ir_channel'], self.dynamic_parameters['ir_imaging'])
            time.sleep(0.3)
            self.camera.acquireSingle(True, os.path.join(self.preparation['name'], 'snaps'))
            self.set_led(self.dynamic_parameters['ir_channel'], self.dynamic_parameters['ir_livefeed'])
            time.sleep(0.2)

            print('A snap image taken')
        else:
            self.camera.acquireSingle(False, '')
            time.sleep(0.1)



    
    def image_series(self, inter_loop_callback=None):
        '''
        For dynamic imaging of pseudopupil movements.

        How this works?
        CameraClient (self.camera) send message to CameraServer to start image acquisition. Starting
        image acquistion takes some time, so to synchronize, we wait a trigger signal from the camera
        before turning the LED on. What is done is essentially half software half hardware triggering.
        Not ideal but seems to work.
        '''
        
        print('Starting dynamic imaging')


        # To speed things up, for the last repeat we didn't wait the ISI. Now here, if the user
        # is very fast, we wait the ISI time.
        try:
            if time.time() < self.isi_slept_time: 
                print('Waiting ISI to be fullfilled from the last run...')
                time.sleep(self.isi_slept_time - time.time())
                print('READY')
        except AttributeError:
            pass
        

        # Create a copy of the dynamic parameters because some of the parameters we tranform to lists
        dynamic_parameters = copy.deepcopy(self.dynamic_parameters)

        # Check that certain variables are actually lists (used for intensity series etc.)
        for param in ['isi', 'flash_on']:
            if type(dynamic_parameters[param]) != type([]):
                # If not a list make it REPEATS lenght list
                dynamic_parameters[param] = [dynamic_parameters[param]] * dynamic_parameters['repeats']
            elif len(dynamic_parameters[param]) != int(dynamic_parameters['repeats']):
                # If a wrong length list (not REPEATS)
                dynamic_parameters[param] = [dynamic_parameters[param][0]] * dynamic_parameters['repeats'] 
        

        for i in range(dynamic_parameters['repeats']):

            imaging_angle = self.reader.get_latest()
            frame_length = dynamic_parameters['frame_length']
            N_frames = int((dynamic_parameters['pre_stim']+dynamic_parameters['stim']+dynamic_parameters['post_stim'])/frame_length)

            label = 'im_pos{}_rep{}'.format(imaging_angle, i)
            print('  Imaging {}'.format(label))

            
            self.set_led(dynamic_parameters['ir_channel'], dynamic_parameters['ir_imaging'])
            time.sleep(0.5)
            
            # Subfolder suffix so if experimenter takes many images from the same position in different conditions
            self.camera.acquireSeries(frame_length, 0, N_frames, label, os.path.join(self.preparation['name'], 'pos{}{}'.format(imaging_angle, dynamic_parameters['suffix']+self.suffix)), 'send')
            
            self.wait_for_trigger()
            time.sleep(dynamic_parameters['pre_stim'])
            
            self.set_led(dynamic_parameters['flash_channel'], dynamic_parameters['flash_on'][i])
            time.sleep(dynamic_parameters['stim'])

            self.set_led(dynamic_parameters['flash_channel'], dynamic_parameters['flash_off'])
            
            time.sleep(dynamic_parameters['post_stim'])
            
            self.set_led(dynamic_parameters['ir_channel'], dynamic_parameters['ir_waiting'])
            
            if i+1 == dynamic_parameters['repeats']:
                self.isi_slept_time = time.time() + dynamic_parameters['isi'][i]
            else:
                time.sleep(dynamic_parameters['isi'][i]-0.5)

        self.set_led(dynamic_parameters['ir_channel'], dynamic_parameters['ir_livefeed'])
        print('DONE!')


    def image_series2(self, inter_loop_callback=None):
         
        print('Starting dynamic imaging')


        # To speed things up, for the last repeat we didn't wait the ISI. Now here, if the user
        # is very fast, we wait the ISI time.
        try:
            if time.time() < self.isi_slept_time: 
                print('Waiting ISI to be fullfilled from the last run...')
                time.sleep(self.isi_slept_time - time.time())
                print('READY')
        except AttributeError:
            pass
        

        # Create a copy of the dynamic parameters because some of the parameters we tranform to lists
        dynamic_parameters = copy.deepcopy(self.dynamic_parameters)

        # Check that certain variables are actually lists (used for intensity series etc.)
        for param in ['isi', 'flash_on']:
            if type(dynamic_parameters[param]) != type([]):
                # If not a list make it REPEATS lenght list
                dynamic_parameters[param] = [dynamic_parameters[param]] * dynamic_parameters['repeats']
            elif len(dynamic_parameters[param]) != int(dynamic_parameters['repeats']):
                # If a wrong length list (not REPEATS)
                dynamic_parameters[param] = [dynamic_parameters[param][0]] * dynamic_parameters['repeats'] 



        for i in range(dynamic_parameters['repeats']):

            imaging_angle = self.reader.get_latest()
            frame_length = dynamic_parameters['frame_length']
            N_frames = int((dynamic_parameters['pre_stim']+dynamic_parameters['stim']+dynamic_parameters['post_stim'])/frame_length)

            label = 'im_pos{}_rep{}'.format(imaging_angle, i)
            print('  Imaging {}'.format(label))
            
            fs = 1000

            stimulus, illumination, camera = get_pulse_stimulus(dynamic_parameters['stim'],
                    dynamic_parameters['pre_stim'], dynamic_parameters['post_stim'],
                    dynamic_parameters['frame_length'], dynamic_parameters['flash_on'][i],
                    dynamic_parameters['ir_imaging'], fs,
                    stimulus_finalval=dynamic_parameters['flash_off'],
                    illumination_finalval=dynamic_parameters['ir_waiting'])
            


            # Subfolder suffix so if experimenter takes many images from the same position in different conditions
            self.camera.acquireSeries(frame_length, 0, N_frames, label, os.path.join(self.preparation['name'], 'pos{}{}'.format(imaging_angle, dynamic_parameters['suffix']+self.suffix)), 'receive')
            
            time.sleep(5)

            
            self.analog_output([dynamic_parameters['flash_channel'],
                    dynamic_parameters['ir_channel'],
                    dynamic_parameters['trigger_channel']],
                    [stimulus, illumination, camera], fs)

            
            if i+1 == dynamic_parameters['repeats']:
                self.isi_slept_time = time.time() + dynamic_parameters['isi'][i]
            else:
                time.sleep(dynamic_parameters['isi'][i]-0.5)

        self.set_led(dynamic_parameters['ir_channel'], dynamic_parameters['ir_livefeed'])
        print('DONE!')

    def image_series3(self, inter_loop_callback=None):

        exit_imaging = False

        print('Starting dynamic imaging')


        # To speed things up, for the last repeat we didn't wait the ISI. Now here, if the user
        # is very fast, we wait the ISI time.
        try:
            if time.time() < self.isi_slept_time: 
                print('Waiting ISI to be fullfilled from the last run...')
                time.sleep(self.isi_slept_time - time.time())
                print('READY')
        except AttributeError:
            pass
        

        # Create a copy of the dynamic parameters because some of the parameters we tranform to lists
        dynamic_parameters = copy.deepcopy(self.dynamic_parameters)

        # Check that certain variables are actually lists (used for intensity series etc.)
        for param in ['isi', 'flash_on']:
            if type(dynamic_parameters[param]) != type([]):
                # If not a list make it REPEATS lenght list
                dynamic_parameters[param] = [dynamic_parameters[param]] * dynamic_parameters['repeats']
            elif len(dynamic_parameters[param]) != int(dynamic_parameters['repeats']):
                # If a wrong length list (not REPEATS)
                dynamic_parameters[param] = [dynamic_parameters[param][0]] * dynamic_parameters['repeats'] 

        imaging_angle = self.reader.get_latest()
        image_directory = os.path.join(self.preparation['name'], 'pos{}{}'.format(imaging_angle, dynamic_parameters['suffix']+self.suffix))
        self.camera.saveDescription(self.preparation['name'], image_directory)

        
        for i in range(dynamic_parameters['repeats']):



            if callable(inter_loop_callback) and inter_loop_callback(i) == False:
                exit_imaging = True
                
            if exit_imaging:
                break
            
            frame_length = dynamic_parameters['frame_length']
            N_frames = int((dynamic_parameters['pre_stim']+dynamic_parameters['stim']+dynamic_parameters['post_stim'])/frame_length)

            label = 'im_pos{}_rep{}'.format(imaging_angle, i)
            print('  Imaging {}'.format(label))
            
            fs = 1000

            stimulus, illumination, camera = get_pulse_stimulus(dynamic_parameters['stim'],
                    dynamic_parameters['pre_stim'], dynamic_parameters['post_stim'],
                    dynamic_parameters['frame_length'], dynamic_parameters['flash_on'][i],
                    dynamic_parameters['ir_imaging'], fs,
                    stimulus_finalval=dynamic_parameters['flash_off'],
                    illumination_finalval=dynamic_parameters['ir_waiting'])
            
            self.set_led(dynamic_parameters['ir_channel'], dynamic_parameters['ir_imaging'])
            time.sleep(0.5)



            # Subfolder suffix so if experimenter takes many images from the same position in different conditions
            self.camera.acquireSeries(frame_length, 0, N_frames, label, image_directory, 'send')
            

            self.analog_output([dynamic_parameters['flash_channel']], [stimulus], fs, wait_trigger=True)

            
            self.set_led(dynamic_parameters['ir_channel'], dynamic_parameters['ir_waiting'])



            if i+1 == dynamic_parameters['repeats']:
                self.isi_slept_time = time.time() + dynamic_parameters['isi'][i]
            else:
                wakeup_time = time.time() + dynamic_parameters['isi'][i]-0.5
                #time.sleep(dynamic_parameters['isi'][i]-0.5)
                while wakeup_time > time.time():
                    if callable(inter_loop_callback) and inter_loop_callback(i) == False:
                        exit_imaging = True
                        break
                    time.sleep(0.01)

        self.set_led(dynamic_parameters['ir_channel'], dynamic_parameters['ir_livefeed'])
        print('DONE!')

    def set_savedir(self, savedir):
        '''
        Set the directory where the taken images are saved.
        '''
        self.camera.setSavingDirectory(savedir)
 
    def set_subfolder_suffix(self, suffix):
        '''
        Set any suffix to a data folder containing the images.
        For example
            pos(-14,0) would become pos(-14,0)_highmag if suffix == "highmag"
        '''
        if suffix:
            self.suffix = '_'+suffix
        else:
            self.suffix = ''

    def initialize(self, name, sex, age):
        '''
        Call this to initialize the experiments.

        name, sex age       Can be '' (empty string)
        '''
        # Preparations, ask droso name
        if name != '':
            self.preparation['name'] = name
        if sex != '':
            self.preparation['sex'] = sex
        if age != '':
            self.preparation['age'] = age

        self.dynamic_parameters = getModifiedParameters()
        print('Preparation name set as {}, sex {}, age {} days.'.format(self.preparation['name'], self.preparation['sex'], self.preparation['age']))

        
        # Saving description file
        desc_string = "name {}\nsex {}\nage {}".format(self.preparation['name'], self.preparation['sex'], self.preparation['age'])
        desc_string += "\n\n#DYNAMIC PROTOCOL PARAMETERS\n"
        for name, value in self.dynamic_parameters.items():
            desc_string += '{} {}\n'.format(name, value)
        print(desc_string)
        self.camera.saveDescription(self.preparation['name'], desc_string)
        
        self.set_led(self.dynamic_parameters['ir_channel'], self.dynamic_parameters['ir_livefeed'])
        self.set_led(self.dynamic_parameters['flash_channel'], self.dynamic_parameters['flash_off'])





    def tick(self):
        '''
        Updates the current angle. In the future may do other houskeeping functions also.
        
        Call this once while in a loop that the angles have to be updated.
        '''
        
        while True:
            
            # Update current angle and echo it to the console
            current_angle = [list(self.reader.read_angles())]
            toDegrees(current_angle)

            if self.previous_angle != current_angle:
                print("Horizontal-vertical is {}".format(current_angle[0]))
                self.previous_angle = current_angle
            else:
                break

        # Run macro if set
        if self.macro:
                
            next_macro_step = False

            action = self.macro[self.i_macro]
            print(action)

            
            if type(action) == type((0,0)):
                # Move motors only if they have reached their positions
                if all([self.motors[i].reached_target() for i in [0,1]]):
                    self.motors[0].move_to(action[0])
                    self.motors[1].move_to(action[1])
                    next_macro_step = True
            if 'wait' in action:
                self.waittime = time.time() + float(action.split(' ')[-1])
                next_macro_step = True
            
            if next_macro_step and self.waittime < time.time():
                self.i_macro += 1
                if self.i_macro == len(self.macro):
                    self.macro = None
                    self.i_macro = 0

    def set_zero(self):
        '''
        Define the current angle pair as the zero point
        (Like tar button on scales)
        '''
        self.reader.current_as_zero()



    def finalize(self):
        '''
        Finalising the experiments, houskeeping stuff.
        '''
        self.set_led(self.dynamic_parameters['ir_channel'], 0)
        self.set_led(self.dynamic_parameters['flash_channel'], 0)

        for motor in self.motors:
            motor.move_to(0)

    def exit(self):
        self.camera.close_server()

    #
    # CONTROLLING LEDS, MOTORS ETC
    #
    
    def move_motor(*args, **kwargs):
        self.reader.move_motor(*args, **kwargs)


    #
    # PROTOCOL QUEUE / MACRO
    #

    @staticmethod
    def list_macros():
        return macro.list_macros()
        
    def run_macro(self, macro_name):
        self.macro = macro.load(macro_name)
        self.i_macro = 0

        


