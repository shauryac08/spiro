# experimenter.py -
#   this file handles running the actual experiments
#

import os
import time
import threading
import numpy as np
from PIL import Image
from io import BytesIO
from datetime import date
from statistics import mean
from collections import deque
from spiro.config import Config
from spiro.logger import log, debug

class Experimenter(threading.Thread):
    def __init__(self, hw=None, cam=None):
        # Initializing the Thread first is generally cleaner
        threading.Thread.__init__(self)
        
        self.hw = hw
        self.cam = cam # This is now your Picamera2 instance
        self.cfg = Config()
        
        self.delay = 60
        self.duration = 7
        self.dir = os.path.expanduser('~')
        self.starttime = 0
        self.endtime = 0
        
        self.running = False
        self.status = "Stopped"
        self.daytime = "TBD"
        self.quit = False
        self.stop_experiment = False
        
        # Threading control
        self.status_change = threading.Event()
        self.next_status = ''
        
        # In Picamera2, managing arrays for preview frames is common 
        # if you are using 'request_preview_frame()'
        self.last_captured = [''] * 4
        self.preview = [''] * 4
        self.preview_lock = threading.Lock()
        
        self.nshots = 0
        self.idlepos = 0


    def stop(self):
        self.status = "Stopping"
        self.next_status = ''
        self.stop_experiment = True
        log("Stopping running experiment...")


    def getDefName(self):
        '''returns a default experiment name'''
        today = date.today().strftime('%Y.%m.%d')
        return today + ' ' + self.cfg.get('name')


    def isDaytime(self):
        '''algorithm for daytime estimation.'''
        
        # In Picamera2, instead of changing resolution and changing it back,
        # we can just capture an array at a specific size directly.
        
        # 1. Set temporary exposure/gain for the test
        # (Using AnalogueGain instead of ISO)
        day_iso = self.cfg.get('dayiso')
        day_shutter = 1000000 // self.cfg.get('dayshutter')
        
        self.cam.set_controls({
            "ExposureTime": day_shutter,
            "AnalogueGain": day_iso / 100.0, # Convert ISO 100 -> 1.0 gain
            "AeEnable": False
        })

        # 2. Capture directly into a NumPy array at the target resolution
        # Picamera2 makes this much cleaner:
        output = self.cam.capture_array(out_size=(320, 240))

        # Calculate the mean pixel intensity
        mean_value = output.mean()
        
        debug("Daytime estimation mean value: " + str(mean_value))
        
        # Return true if it's "bright enough"
        return mean_value > 10

    def setWB(self):
        debug("Determining white balance.")
    
    # 1. Enable Auto White Balance
    # In libcamera, 'Auto' is mode 0. 
    # Valid modes: Auto, Incandescent, Tungsten, Fluorescent, Indoor, Daylight, Cloudy, Custom
        self.cam.set_controls({"AwbEnable": True})
    
    # 2. Wait for the AWB algorithm to settle
        time.sleep(2)
    
    # 3. Retrieve the current gains from metadata
    # metadata['ColourGains'] returns a tuple (RedGain, BlueGain)
        metadata = self.cam.capture_metadata()
        gains = metadata.get('ColourGains', (1.0, 1.0))

    # 4. Disable AWB and lock the gains
    # We set AwbEnable to False and manually apply the gains we just read
        self.cam.set_controls({
            "AwbEnable": False,
            "ColourGains": gains
        })

        debug(f"Locked White Balance gains at Red: {gains[0]:.2f}, Blue: {gains[1]:.2f}")


    def takePicture(self, name, plate_no):
        filename = ""
        prev_daytime = self.daytime
        self.daytime = self.isDaytime()
        
        # Setup Exposure and Gain based on time of day
        if self.daytime:
            time.sleep(0.5)
            shutter = 1000000 // self.cfg.get('dayshutter')
            gain = self.cfg.get('dayiso') / 100.0
            filename = os.path.join(self.dir, name + "-day.png")
            self.cam.set_controls({"ExposureTime": shutter, "AnalogueGain": gain, "ColorEffects": None})
        else:
            self.hw.LEDControl(True)
            time.sleep(0.5)
            shutter = 1000000 // self.cfg.get('nightshutter')
            gain = self.cfg.get('nightiso') / 100.0
            filename = os.path.join(self.dir, name + "-night.png")
            self.cam.set_controls({"ExposureTime": shutter, "AnalogueGain": gain})
        
        # White Balance Logic
        if prev_daytime != self.daytime and self.daytime:
            # Check if AWB is not locked (logic depends on your current app state)
            self.setWB()
        
        debug("Capturing %s" % filename)
        
        # Disable Auto Exposure for the capture
        self.cam.set_controls({"AeEnable": False})
        
        # Capture directly to a numpy array (skips the BytesIO mess)
        # Picamera2 handles the padding/cropping internally!
        array = self.cam.capture_array()
        
        # Turn off LED
        if not self.daytime:
            self.hw.LEDControl(False)
        
        # Convert to PIL Image
        im = Image.fromarray(array)
        im.save(filename)
        
        # Create thumbnails for preview
        with self.preview_lock:
            thumb = im.copy()
            thumb.thumbnail((800, 600))
            # Save to your existing preview list
            buf = io.BytesIO()
            thumb.save(buf, format="JPEG")
            self.preview[plate_no] = buf
        
        self.last_captured[plate_no] = filename
        
        # Reset for next run
        self.cam.set_controls({"AeEnable": True, "ExposureTime": 0})
        
        def run(self):
            '''starts experiment if there is signal to do so'''
            while not self.quit:
                self.status_change.wait()
                if self.next_status == 'run':
                    self.next_status = ''
                    self.status_change.clear()
                    self.runExperiment()


    def go(self):
        '''signals intent to start experiment'''
        self.next_status = 'run'
        self.status_change.set()                


    def runExperiment(self):
        '''main experiment loop'''
        if self.running:
            raise RuntimeError('An experiment is already running.')
    
        try:
            debug("Starting experiment.")
            self.running = True
            self.status = "Initiating"
            self.starttime = time.time()
            self.endtime = time.time() + 60 * 60 * 24 * self.duration
            self.last_captured = [''] * 4
            self.delay = self.delay or 0.001
            self.nshots = self.duration * 24 * 60 // self.delay
            
            # --- PICAMERA2 UPDATES ---
            # "auto" exposure is now AeEnable: True
            # shutter_speed = 0 is also handled by enabling AE
            self.cam.set_controls({
                "AeEnable": True,
                "ExposureTime": 0 
            })
            # -------------------------
            
            self.hw.LEDControl(False)
    
            # Directory setup logic remains the same...
            if self.dir == os.path.expanduser('~'):
                self.dir = os.path.join(os.path.expanduser('~'), self.getDefName())
    
            for i in range(4):
                platedir = "plate" + str(i + 1)
                os.makedirs(os.path.join(self.dir, platedir), exist_ok=True)
    
            while time.time() < self.endtime and not self.stop_experiment:
                loopstart = time.time()
                nextloop = time.time() + 60 * self.delay
                if nextloop > self.endtime:
                    nextloop = self.endtime
    
                for i in range(4):
                    # Motor rotation logic remains the same...
                    if i == 0:
                        self.hw.motorOn(True)
                        self.status = "Finding start position"
                        self.hw.findStart(calibration=self.cfg.get('calibration'))
                        if self.status != "Stopping": self.status = "Imaging"
                    else:
                        self.hw.halfStep(100, 0.03)
    
                    time.sleep(0.5)
    
                    now = time.strftime("%Y%m%d-%H%M%S", time.localtime())
                    name = os.path.join("plate" + str(i + 1), "plate" + str(i + 1) + "-" + now)
                    
                    # This uses the refactored takePicture from the previous step
                    self.takePicture(name, i)
    
                self.nshots -= 1
                self.hw.motorOn(False)
                if self.status != "Stopping": self.status = "Waiting"
    
                # Idle rotation logic remains the same...
                if self.idlepos > 0:
                    self.hw.motorOn(True)
                    self.hw.halfStep(50 * self.idlepos, 0.03)
                    self.hw.motorOn(False)
    
                self.idlepos += 1
                if self.idlepos > 7:
                    self.idlepos = 0
    
                while time.time() < nextloop and not self.stop_experiment:
                    time.sleep(1)
    
        finally:
            log("Experiment stopped.")
            # --- PICAMERA2 CLEANUP ---
            self.cam.set_controls({
                "ColorEffects": None,
                "AeEnable": True,
                "AeMeteringMode": 1 # 1 is 'Spot'
            })
            # -------------------------
            self.status = "Stopped"
            self.stop_experiment = False
            self.running = False
