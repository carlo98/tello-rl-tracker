"""
tellotracker:
Allows manual operation of the drone and demo tracking mode.

Requires mplayer to record/save video.

Controls:
- tab to lift off
- WASD to move the drone
- arrow keys to ascend, descend, or yaw quickly
- R to start recording video, R again to stop recording
  (video and photos will be saved to a timestamped file in ~/Pictures/)
- Z to toggle camera zoom state
  (zoomed-in widescreen or high FOV 4:3)
@author Leonie Buckley, Saksham Sinha and Jonathan Byrne
@copyright 2018 see license file for details

   IMPORTANT: Only one feature (1, 2) can be activate at any time.
 - 1 to toggle collision avoidance
 - 2 to toggle tracking
 - 3 to toggle reinforcement learning training for collision avoidance (If activated then also collision avoidance will be ON)
 - 4 to toggle go back to starting point
 - x to end/start episode of RL
 - F to save frame as free (collision avoidance)
 - B to save frame as blocked (collision avoidance) 
"""
import time
import datetime
import os
import copy
from djitellopy.tello import Tello
import numpy as np
import av
import cv2
from pynput import keyboard
from Face_Recognition.face_rec_tracker import Tracker
from Collision_Avoidance.collision_avoidance import Agent
from Collision_Avoidance.RL import RL_Agent
from Camera_Calibration.process_image import FrameProc
from Localization.pose_estimation import Pose
from scipy.interpolate import interp1d
import sys
import traceback
import threading


def main():
    """ Create a tello controller and show the video feed."""
    tellotrack = TelloCV()
    
    try:
        # skip first 300 frames
        frame_skip = 300
        while True:
            for frame in tellotrack.container.decode(video=0):
                if 0 < frame_skip:
                    frame_skip = frame_skip - 1
                    continue
                start_time = time.time()
                image = tellotrack.process_frame(frame)
                show(image)
                if frame.time_base < 1.0/60:
                    time_base = 1.0/60
                else:
                    time_base = frame.time_base
                
                frame_skip = int((time.time() - start_time)/time_base)
    except Exception as ex:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        traceback.print_exception(exc_type, exc_value, exc_traceback)
        print(ex)
    finally:
        cv2.destroyAllWindows()


def show(frame):
    """show the frame to cv2 window"""
    cv2.imshow("Frame", frame)
    key = cv2.waitKey(1) & 0xFF

    # if the 'q' key is pressed, stop the loop
    if key == ord("q"):
        exit()


class TelloCV(object):
    """
    TelloTracker builds keyboard controls on top of TelloPy as well
    as generating images from the video stream and enabling opencv support
    """

    def __init__(self):
        self.prev_flight_data = None
        self.record = False
        self.tracking = False
        self.keydown = False
        self.date_fmt = '%Y-%m-%d_%H%M%S'
        self.speed = 30
        self.speed_hand = 60
        if os.path.isdir('Collision_Avoidance/data'):
            if not os.path.isdir('Collision_Avoidance/data/blocked') or not os.path.isdir('Collision_Avoidance/data/free'):
                print("Either 'blocked' folder or 'free' folder or both don't exist, any attempt to save images for NN training will fail!")
        else:
            print("'data' folder doesn't exists, any attempt to save images for NN training will fail!")
        self.avoidance = False
        self.rl_training = False
        self.reward = 0
        self.episode_cont = 1
        self.current_step = 0
        self.old_state = None
        self.current_state = None
        self.train_rl_sem = threading.Semaphore(1)
        self.episode_start = True
        self.eps_pose = 1e-5
        self.save_frame = False
        self.blocked_free = 0
        self.distance = 70
        self.area_min = 4000
        self.area_max = 8000
        self.track_cmd = ""
        self.ca_agent = Agent()
        self.rl_agent = RL_Agent(self.ca_agent.model, self.ca_agent.device)
        self.tracker = Tracker()
        self.pose_estimator = Pose()
        self.drone = Tello()
        self.init_drone()
        self.init_controls()

        # container for processing the packets into frames
        self.container = av.open(self.drone.get_video_capture())
        self.vid_stream = self.container.streams.video[0]
        self.out_file = None
        self.out_stream = None
        self.out_name = None
        self.start_time = time.time()
        self.video_initialized = False
        self.frameproc = FrameProc(self.vid_stream.width, self.vid_stream.height)

    def init_drone(self):
        """Connect, uneable streaming and subscribe to events"""
        # self.drone.log.set_level(2)
        self.drone.connect()
        self.drone.streamon()

    def on_press(self, keyname):
        """handler for keyboard listener"""
        if self.keydown:
            return
        try:
            self.keydown = True
            keyname = str(keyname).strip('\'')
            print('+' + keyname)
            if keyname == 'Key.esc':
                self.drone.land()
                exit(0)
            if keyname in self.controls:
                key_handler = self.controls[keyname]
                if keyname in ['1', '2', '3', 'x']:
                        key_handler(self.speed)
                else:
                    if keyname == 'w':
                        self.pose_estimator.move(speed=self.speed_hand)
                    elif keyname == 's':
                        self.pose_estimator.move(speed=-self.speed_hand)
                    elif keyname == 'Key.left':
                        self.pose_estimator.move(yaw=-self.speed_hand)
                    elif keyname == 'Key.right':
                        self.pose_estimator.move(yaw=self.speed_hand)
                    elif keyname == 'Key.down':
                        self.pose_estimator.move(speed=-self.speed_hand, vertical=True)	
                    elif keyname == 'Key.up':
                        self.pose_estimator.move(speed=self.speed_hand, vertical=True)
                    key_handler(self.speed_hand)
        except AttributeError:
            print('special key {0} pressed'.format(keyname))

    def on_release(self, keyname):
        """Reset on key up from keyboard listener"""
        self.keydown = False
        keyname = str(keyname).strip('\'')
        print('-' + keyname)
        if keyname in self.controls and keyname != 'x':
            key_handler = self.controls[keyname]
            key_handler(0)

    def init_controls(self):
        """Define keys and add listener"""
        self.controls = {
            'w': lambda speed: self.drone.send_rc_control(0, speed, 0, 0),
            's': lambda speed: self.drone.send_rc_control(0, -speed, 0, 0),
            'a': lambda speed: self.drone.send_rc_control(-speed, 0, 0, 0),
            'd': lambda speed: self.drone.send_rc_control(speed, 0, 0, 0),
            'i': lambda speed: self.drone.flip_forward(),
            'k': lambda speed: self.drone.flip_back(),
            'j': lambda speed: self.drone.flip_left(),
            'l': lambda speed: self.drone.flip_right(),
            # arrow keys for fast turns and altitude adjustments
            'Key.left': lambda speed: self.drone.send_rc_control(0, 0, 0, speed),
            'Key.right': lambda speed: self.drone.send_rc_control(0, 0, 0, -speed),
            'Key.up': lambda speed: self.drone.send_rc_control(0, 0, speed, 0),
            'Key.down': lambda speed: self.drone.send_rc_control(0, 0, -speed, 0),
            'Key.tab': lambda speed: self.drone.takeoff(),
            'Key.backspace': lambda speed: self.drone.land(),
            'r': lambda speed: self.toggle_recording(speed),
            'z': lambda speed: self.toggle_zoom(speed),
            'b': lambda speed: self.toggle_blocked_free(0),
            'f': lambda speed: self.toggle_blocked_free(1),
            '1': lambda speed: self.toggle_collisionAvoidance(speed),
            '2': lambda speed: self.toggle_tracking(speed),
            '3': lambda speed: self.toggle_rl_training(speed),
            '4': lambda speed: self.toggle_go_back(speed)
            # Reinforcement learning commands
            'x': lambda speed: self.toggle_episode_done(True),
        }
        self.key_listener = keyboard.Listener(on_press=self.on_press,
                                              on_release=self.on_release)
        self.key_listener.start()
        
    def interpolate_readings(self, raw_readings):
        """
        Predicts next position of target
        """
        readings = []
        readings_index = []
        flag = True # Set to false if last reading has no face
        for i, reading in enumerate(raw_readings):
            if reading[2] != 0:
                readings.append(reading)
                readings_index.append(i)
            elif i == len(raw_readings)-1:
                flag = False

        if len(readings) >= 2:
            readings = np.array(readings)
            fx = interp1d(readings_index, readings[:, 0], fill_value="extrapolate")
            fy = interp1d(readings_index, readings[:, 1], fill_value="extrapolate")
            farea = interp1d(readings_index, readings[:, 2], fill_value="extrapolate")
            return fx(len(raw_readings)), fy(len(raw_readings)), farea(len(raw_readings))
            
        # If only one reading available using it only if it is the most recent one
        if len(readings) == 1 and flag:
            return readings[0][0], readings[0][1], readings[0][2]

        return -1, -1, -1

    def process_frame(self, frame):
        """converts frame to cv2 image and show"""
        
        x = np.array(frame.to_image())
        # Get undistorted frame
        x = self.frameproc.undistort_frame(x)
        
        if not self.video_initialized:
            self.tracker.init_video(x.shape[0], x.shape[1])
            self.video_initialized = True
         
        image = cv2.cvtColor(copy.deepcopy(x), cv2.COLOR_RGB2BGR)
        image = self.write_hud(image)
        if self.record:
            self.record_vid(frame)

        cmd = ""
        if self.save_frame:
            if self.blocked_free == 0:
                cv2.imwrite("Collision_Avoidance/data/blocked/"+datetime.datetime.now().strftime(self.date_fmt)+".png", x)
            elif self.blocked_free == 1:
                cv2.imwrite("Collision_Avoidance/data/free/"+datetime.datetime.now().strftime(self.date_fmt)+".png", x)
            self.save_frame = False
            
        ## Start Collision Avoidance code
        if self.avoidance:
            cmd_ca_agent, display_frame = self.ca_agent.track(x)
            if cmd_ca_agent == 1:
                self.drone.send_rc_control(0, 0, 0, self.speed)
                self.track_cmd = "clockwise"
            else:
                self.drone.send_rc_control(0, self.speed, 0, 0)
                self.track_cmd = "forward"
                
            ## Start Reinforcement Learning code
            if self.rl_training and self.episode_start:
                self.current_state = display_frame.get()
                if self.current_state is not None and self.old_state is not None:
                    if self.track_cmd == "forward":  # Reward each forward movement
                        new_reward = 1 / self.rl_agent.max_steps
                        self.reward += new_reward
                    else:
                        new_reward = 0
                    self.train_rl_sem.acquire()
                    self.rl_agent.appendMemory(self.old_state, (lambda action: 0 if self.track_cmd == 'clockwise' else 1)(self.track_cmd), new_reward, self.current_state, 0)
                    self.train_rl_sem.release()
                    if self.current_step >= self.rl_agent.max_steps:
                        self.toggle_episode_done(False)
                    self.current_step += 1
                self.old_state = copy.deepcopy(self.current_state)
            ## End Reinforcement Learning code
                
            image = display_frame
        ## End Collision Avoidance code
        
        ## Start Tracking code
        elif self.tracking:
            readings, display_frame = self.tracker.track(image)
            xoff, yoff, distance_measure = self.interpolate_readings(copy.deepcopy(readings))
            if xoff == -1:
                if self.track_cmd is not "":
                    self.drone.send_rc_control(0, 0, 0, 0)
                    self.track_cmd = ""
            elif xoff < -self.distance:
                cmd = "counter_clockwise"
                self.drone.send_rc_control(0, 0, 0, -self.speed)
            elif xoff > self.distance:
                cmd = "clockwise"
                self.drone.send_rc_control(0, 0, 0, self.speed)
            elif yoff < -self.distance:
                cmd = "down"
                self.drone.send_rc_control(0, 0, -self.speed, 0)
            elif yoff > self.distance:
                cmd = "up"
                self.drone.send_rc_control(0, 0, self.speed, 0)
            elif distance_measure <= self.area_min:
                print("Forward ", distance_measure)
                cmd = "forward"
                self.drone.send_rc_control(0, self.speed, 0, 0)
            elif distance_measure >= self.area_max:
                print("backward ", distance_measure)
                cmd = "backward"
                self.drone.send_rc_control(0, -self.speed, 0, 0)
            else:
                if self.track_cmd is not "":
                    self.drone.send_rc_control(0, 0, 0, 0)
                    self.track_cmd = ""
            
            image = display_frame
        ## End Tracking code
        
        if cmd is not self.track_cmd:
            if cmd is not "":
                print("track command:", cmd)
                self.track_cmd = cmd

        return image

    def write_hud(self, frame):
        """Draw drone info, tracking and record on frame"""
        stats = self.prev_flight_data.split('|')
        stats.append("Tracking:" + str(self.tracking))
        stats.append("Collision Avoidance NN:" + str(self.avoidance))
        stats.append("RL Training:" + str(self.rl_training))
        if self.drone.zoom:
            stats.append("VID")
        else:
            stats.append("PIC")
        if self.record:
            diff = int(time.time() - self.start_time)
            mins, secs = divmod(diff, 60)
            stats.append("REC {:02d}:{:02d}".format(mins, secs))

        for idx, stat in enumerate(stats):
            text = stat.lstrip()
            cv2.putText(frame, text, (0, 30 + (idx * 30)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        1.0, (255, 0, 0), lineType=30)
        return frame
        
    def toggle_blocked_free(self, block_free):
        self.save_frame = True
        self.blocked_free = block_free

    def toggle_recording(self, speed):
        """Handle recording keypress, creates output stream and file"""
        if speed == 0:
            return
        self.record = not self.record

        if self.record:
            datename = [os.getenv('HOME'), datetime.datetime.now().strftime(self.date_fmt)]
            self.out_name = '{}/Pictures/tello-{}.mp4'.format(*datename)
            print("Outputting video to:", self.out_name)
            self.out_file = av.open(self.out_name, 'w')
            self.start_time = time.time()
            self.out_stream = self.out_file.add_stream(
                'mpeg4', self.vid_stream.rate)
            self.out_stream.pix_fmt = 'yuv420p'
            self.out_stream.width = self.vid_stream.width
            self.out_stream.height = self.vid_stream.height

        if not self.record:
            print("Video saved to ", self.out_name)
            self.out_file.close()
            self.out_stream = None

    def record_vid(self, frame):
        """
        convert frames to packets and write to file
        """
        new_frame = av.VideoFrame(
            width=frame.width, height=frame.height, format=frame.format.name)
        for i in range(len(frame.planes)):
            new_frame.planes[i].update(frame.planes[i])
        pkt = None
        try:
            pkt = self.out_stream.encode(new_frame)
        except IOError as err:
            print("encoding failed: {0}".format(err))
        if pkt is not None:
            try:
                self.out_file.mux(pkt)
            except IOError:
                print('mux failed: ' + str(pkt))

    def toggle_tracking(self, speed):
        """ Handle tracking keypress"""
        if speed == 0:  # handle key up event
            return
        self.tracking = not self.tracking
        self.avoidance = False
        self.rl_training = False
        print("tracking:", self.tracking)
        
    def toggle_collisionAvoidance(self, speed):
        """ Handle avoidance keypress"""
        if speed == 0:  # handle key up event
            return
        self.avoidance = not self.avoidance
        self.tracking = False
        print("avoidance:", self.avoidance)
        
    def toggle_go_back(self, speed):
        """ Handle go back to origin keypress """
        return_path = self.pose_estimator.go_back_same_path()
        for movement in path:
            p.move(speed=movement[0], yaw=movement[1], vertical = True if movement[2] != 0 else False)
            if movement[1] > self.eps_pose:
                cmd = "clockwise"
                self.drone.send_rc_control(0, 0, 0, movement[1])
            elif movement[1] < -self.eps_pose:
                cmd = "counter_clockwise"
                self.drone.send_rc_control(0, 0, 0, movement[1])
            elif movement[0] < -self.eps_pose and not movement[2]:
                cmd = "backward"
                self.drone.send_rc_control(0, movement[0], 0, 0)
            elif movement[0] > self.eps_pose and not movement[2]:
                cmd = "forward"
                self.drone.send_rc_control(0, movement[0], 0, 0)
            elif movement[0] > self.eps_pose and movement[2]:
                cmd = "up"
                self.drone.send_rc_control(0, 0, movement[0], 0)
            elif movement[0] < -self.eps_pose and movement[2]:
                cmd = "down"
                self.drone.send_rc_control(0, 0, movement[0], 0)
            self.track_cmd = cmd
        
    def toggle_rl_training(self, speed):
        """ Handle reinforcement learning training keypress """
        if speed == 0:  # handle key up event
            return
        self.rl_training = not self.rl_training
        self.avoidance = self.rl_training
        self.tracking = False
        print("RL training:", self.rl_training)
        print("avoidance:", self.avoidance)
        
    def toggle_episode_done(self, collision):
        """
        RL episode finished, either max number of steps or collision detected.
        """
        if self.episode_start:
            if self.track_cmd is not "":
                self.drone.send_rc_control(0, 0, movement[0], 0)
                self.track_cmd = ""
            self.speed = 0
            self.train_rl_sem.acquire()
            if collision:
                print("Collision detected by you, great work!")
                self.reward -= 1
                self.rl_agent.appendMemory(self.old_state, (lambda action: 0 if self.track_cmd == 'clockwise' else 1)(self.track_cmd), -1, self.current_state, 1)
            else:
                self.rl_agent.appendMemory(self.old_state, (lambda action: 0 if self.track_cmd == 'clockwise' else 1)(self.track_cmd), 0, self.current_state, 1)
                print("Episode completed, good Tommy!")
            print("Episode ", self.episode_cont, " reward: ", self.reward)
            self.rl_agent.update_model(self.ca_agent.model, self.episode_cont)
            self.rl_agent.save_model(self.ca_agent.model, self.episode_cont)
            self.train_rl_sem.release()
            self.episode_start = False
        else:
            print("Episode Start")
            self.episode_start = True
            self.speed = 30
            self.episode_cont += 1
            self.reward = 0
            self.current_step = 0

    def toggle_zoom(self, speed):
        """
        In "video" mode the self.drone sends 1280x720 frames.
        In "photo" mode it sends 2592x1936 (952x720) frames.
        The video will always be centered in the window.
        In photo mode, if we keep the window at 1280x720 that gives us ~160px on
        each side for status information, which is ample.
        Video mode is harder because then we need to abandon the 16:9 display size
        if we want to put the HUD next to the video.
        """
        if speed == 0:
            return
        self.drone.set_video_mode(not self.drone.zoom)

    def flight_data_handler(self, event, sender, data):
        """Listener to flight data from the drone."""
        text = str(data)
        if self.prev_flight_data != text:
            self.prev_flight_data = text

    def handle_flight_received(self, event, sender, data):
        """Create a file in ~/Pictures/ to receive image from the drone"""
        path = '%s/Pictures/tello-%s.jpeg' % (
            os.getenv('HOME'),
            datetime.datetime.now().strftime(self.date_fmt))
        with open(path, 'wb') as out_file:
            out_file.write(data)
        print('Saved photo to %s' % path)


if __name__ == '__main__':
    main()
