
import os

import numpy as np
import json

try:
    import scipy.signal
except ModuleNotFoundError:
    scipy = None

try:
    from biosystfiles import extract as bsextract
except ModuleNotFoundError:
    bsextract = None

from .directories import CODE_ROOTDIR

class StimulusBuilder:
    '''
    Get various stimulus waveforms
    - to the stimulus LED
    - and on pulse for illumination LED
    - and square wave for triggering the camera.
    
    
    '''

    def __init__(self, stim_time, prestim_time, poststim_time, frame_length,
            stimulus_intensity, illumination_intensity, fs,
            stimulus_finalval=0, illumination_finalval=0,
            wtype='square'):
            '''
            stim_time               The time stimulus LED is on
            prestim_time            The time the camera is running and illumination is on before the stimulus
            poststim_time           The time the camera is running and illumination is on after the stimulus
            stimulus_intensity      From 0 to 1, the brightness of the stimulus
            illumination_intensity  From 0 to 1, the brightness of the illumination lights
            wtype                   "square" or "sinelogsweep" or "squarelogsweep"

            '''

            self.stim_time = stim_time
            self.prestim_time = prestim_time
            self.poststim_time = poststim_time
            self.frame_length = frame_length
            self.stimulus_intensity = stimulus_intensity
            self.illumination_intensity = illumination_intensity
            self.fs = fs
            self.stimulus_finalval = stimulus_finalval
            self.illumination_finalval = illumination_finalval

            self.wtype = wtype

            self.N_frames = int(round((stim_time+prestim_time+poststim_time)/frame_length))

            self.overload_stimulus = None
            

    def overload_biosyst_stimulus(self, fn, channel=0):
        '''
        Loads a Biosyst stimulus that gets returned then at
        get_stimulus_pulse instead.

        Returns the overload stimulus and new fs
        '''
        
        if fn.endswith('.json'):
            ffn = os.path.join(CODE_ROOTDIR, 'biosyst_stimuli', fn)
            with open(fnn, 'r') as fp:
                data = json.load(fp)

            self.fs = data['fs']
            self.overload_stimulus = []

            for i_stim in range(10):
                key = f'stim_{i_stim}'
                if key not in data:
                    continue
                self.overload_stimulus.append(np.array(data[key]))

            return self.overload_stimulus[0], self.fs

        if bsextract is None:
            raise ModuleNotFoundError('Module required\npip install python-biosystfiles')

        ffn = os.path.join(CODE_ROOTDIR, 'biosyst_stimuli', fn)
        self.overload_stimulus, self.fs = bsextract(ffn, channel)
        self.overload_stimulus = self.overload_stimulus.flatten()
        print(self.overload_stimulus.shape)
        print(np.max(self.overload_stimulus))

        return self.overload_stimulus, self.fs
    
    def get_stimulus_pulse(self):
        '''
        Constant value pulse

                _________stimulus_intensity
                |       |
        ________|       |__________
        prestim   stim    poststim
        '''

        if self.overload_stimulus is not None:
            return self.overload_stimulus

        N0_samples = int(self.prestim_time*self.fs)
        N1_samples = int(self.stim_time*self.fs)
        N2_samples = int(self.poststim_time*self.fs)
        
        if self.wtype == 'square':
            stimulus = np.concatenate( (np.zeros(N0_samples), np.ones(N1_samples), np.zeros(N2_samples)) )
        elif 'logsweep' in self.wtype:
            try:
                wtype, f0, f1 = self.wtype.split(',')
                f0 = float(f0)
                f1 = float(f1)
            except:
                print("Doing logsweep from 0.5 Hz to 100 Hz")
                f0=0.5
                f1=100
                wtype = self.wtype
            
            times = np.linspace(0, self.stim_time, N1_samples)
            active = scipy.signal.chirp(times, f0=f0, f1=f1, t1=self.stim_time, phi=-90, method='logarithmic')
            
            if wtype == 'squarelogsweep':
                active[active>0] = 1
                active[active<0] = -1
            elif wtype == '3steplogsweep':
                cstep = np.sin(np.pi/4)
                active[np.abs(active) <= cstep] = 0
                active[active > cstep] = 1
                active[active < -cstep] = -1
                
            elif wtype == 'sinelogsweep':
                pass
            else:
                raise ValueError('Unkown flash_type'.format(wtype))
                
            # Join with pre and post 0.5 values
            # and move and scale between 0 and 1 (from - 1 and 1)
            stimulus = np.concatenate( (np.ones(N0_samples)/2, (active+1)/2, np.ones(N2_samples)/2) )
            
        else:
            raise ValueError('Invalid wtype given, has to be "square" or "sinelogsweep" or "3steplogsweep"')

        stimulus = self.stimulus_intensity * stimulus

   
        stimulus[-1] = self.stimulus_finalval
        
        return stimulus



    def get_illumination(self):
        '''
        Returns 1D np.array.
        '''
        illumination = np.ones( int((self.stim_time+self.prestim_time+self.poststim_time)*self.fs) )
        illumination = self.illumination_intensity * illumination

        illumination[-1] = self.illumination_finalval
        
        return illumination



    def get_camera(self):
        '''
        Get square wave camera triggering stimulus.
        
        Returns 1D np.array.
        '''
        
        samples_per_frame = int(frame_length * fs /2)
        
        camera = np.concatenate( ( np.ones((samples_per_frame, self.N_frames)), np.zeros((samples_per_frame, self.N_frames)) ) ).T.flatten()
        camera = 5*camera
    
        camera[-1] = 0

        return camera



def main():
    '''Saves stimulus as a json.
    '''
    from .imaging_parameters import getModifiedParameters

    data = {}
    for i_stim in range(10):

        dynamic_parameters = getModifiedParameters()

        fs = 10000
        builder = StimulusBuilder(
                dynamic_parameters['stim'],
                dynamic_parameters['pre_stim'],
                dynamic_parameters['post_stim'],
                dynamic_parameters['frame_length'],
                dynamic_parameters['flash_on'],
                dynamic_parameters['ir_imaging'],
                fs,
                stimulus_finalval=dynamic_parameters['flash_off'],
                illumination_finalval=dynamic_parameters['ir_waiting'],
                wtype=dynamic_parameters['flash_type'])

        if dynamic_parameters.get('biosyst_stimulus', ''):
            bsstim, fs = builder.overload_biosyst_stimulus(
                    dynamic_parameters['biosyst_stimulus'], dynamic_parameters['biosyst_channel'])
            #N_frames = int(round((len(bsstim)/fs) / dynamic_parameters['frame_length']))

        stimulus = builder.get_stimulus_pulse().tolist()

        data['fs'] = fs
        data[f'stim_{i_stim}'] = stimulus

        cont = input('Add more (y/n)')

        if cont.lower().startswith('n'):
            break

    with open('test.json', 'w') as fp:
        json.dump(data, fp)


if __name__ == "__main__":
    main()
