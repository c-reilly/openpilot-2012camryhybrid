#!/usr/bin/env python
from common.realtime import sec_since_boot
from cereal import car, log
from selfdrive.config import Conversions as CV
from selfdrive.controls.lib.drive_helpers import EventTypes as ET, create_event
from selfdrive.controls.lib.vehicle_model import VehicleModel
from selfdrive.car.oldcar.carstate import CarState, get_can_parser
from selfdrive.car.oldcar.values import ECU, check_ecu_msgs, CAR
from selfdrive.swaglog import cloudlog
import time

try:
  from selfdrive.car.oldcar.carcontroller import CarController
except ImportError:
  CarController = None


class CarInterface(object):
  def __init__(self, CP, sendcan=None):
    self.CP = CP
    self.VM = VehicleModel(CP)

    self.frame = 0
    self.gas_pressed_prev = False
    self.brake_pressed_prev = False
    self.can_invalid_count = 0
    self.cruise_enabled_prev = False
    
    # Double cruise stalk pull enable
    # 2 taps of on/off will enable/disable
    self.last_cruise_stalk_pull_time = 0
    self.cruise_stalk_pull_time = 0
    #self.cruise_stalk_pull = False in carstate
    self.last_cruise_stalk_pull = False
    self.double_stalk_pull = False
    self.user_enabled = False
    self.current_time = 0
    self.last_angle_steers = 0
    
    #CAN check between arduino and OP
    self.can_check = 0
    self.last_can_check_time = 0

    # *** init the major players ***
    self.CS = CarState(CP)

    self.cp = get_can_parser(CP)

    # sending if read only is False
    if sendcan is not None:
      self.sendcan = sendcan
      self.CC = CarController(self.cp.dbc_name, CP.carFingerprint, CP.enableCamera, CP.enableDsu, CP.enableApgs)

  @staticmethod
  def compute_gb(accel, speed):
    return float(accel) / 3.0

  @staticmethod
  def calc_accel_override(a_ego, a_target, v_ego, v_target):
    return 1.0

  @staticmethod
  def get_params(candidate, fingerprint):

    # kg of standard extra cargo to count for drive, gas, etc...
    std_cargo = 136

    ret = car.CarParams.new_message()

    ret.carName = "oldcar"
    ret.carFingerprint = candidate

    ret.safetyModel = car.CarParams.SafetyModels.toyota

    # pedal
    ret.enableCruise = True

    # FIXME: hardcoding honda civic 2016 touring params so they can be used to
    # scale unknown params for other cars
    mass_civic = 2923 * CV.LB_TO_KG + std_cargo
    wheelbase_civic = 2.70
    centerToFront_civic = wheelbase_civic * 0.4
    centerToRear_civic = wheelbase_civic - centerToFront_civic
    rotationalInertia_civic = 2500
    tireStiffnessFront_civic = 192150
    tireStiffnessRear_civic = 202500

    ret.steerKiBP, ret.steerKpBP = [[0.], [0.]]
    ret.steerActuatorDelay = 0.12 #0.16  # Default delay, Prius has larger delay

    if candidate == CAR.COROLLA:
      ret.safetyParam = 100 # see conversion factor for STEER_TORQUE_EPS in dbc file
      ret.wheelbase = 2.70
      ret.steerRatio = 17.8
      tire_stiffness_factor = 0.444
      ret.mass = 2860 * CV.LB_TO_KG + std_cargo  # mean between normal and hybrid
      ret.steerKpV, ret.steerKiV = [[0.2], [0.05]]
      ret.steerKf = 0.00003   # full torque for 20 deg at 80mph means 0.00007818594

    elif candidate == CAR.CAMRYH:
      ret.safetyParam = 100
      ret.wheelbase = 2.77622
      ret.steerRatio = 18.0 #14.8
      tire_stiffness_factor = 0.7933
      ret.mass = 3400 * CV.LB_TO_KG + std_cargo #mean between normal and hybrid
      ret.steerKpV, ret.steerKiV = [[0.6], [0.1]]
      ret.steerKf = 0.00006

    ret.steerRateCost = 1.
    ret.centerToFront = ret.wheelbase * 0.44

    ret.longPidDeadzoneBP = [0., 9.]
    ret.longPidDeadzoneV = [0., .15]

    # min speed to enable ACC. if car can do stop and go, then set enabling speed
    # to a negative value, so it won't matter.
    # hybrid models can't do stop and go even though the stock ACC can't
    if candidate in [CAR.CAMRYH]:
      ret.minEnableSpeed = -1.
    elif candidate in [CAR.COROLLA]: # TODO: hack ICE to do stop and go
      ret.minEnableSpeed = 19. * CV.MPH_TO_MS

    centerToRear = ret.wheelbase - ret.centerToFront
    # TODO: get actual value, for now starting with reasonable value for
    # civic and scaling by mass and wheelbase
    ret.rotationalInertia = rotationalInertia_civic * \
                            ret.mass * ret.wheelbase**2 / (mass_civic * wheelbase_civic**2)

    # TODO: start from empirically derived lateral slip stiffness for the civic and scale by
    # mass and CG position, so all cars will have approximately similar dyn behaviors
    ret.tireStiffnessFront = (tireStiffnessFront_civic * tire_stiffness_factor) * \
                             ret.mass / mass_civic * \
                             (centerToRear / ret.wheelbase) / (centerToRear_civic / wheelbase_civic)
    ret.tireStiffnessRear = (tireStiffnessRear_civic * tire_stiffness_factor) * \
                            ret.mass / mass_civic * \
                            (ret.centerToFront / ret.wheelbase) / (centerToFront_civic / wheelbase_civic)

    # no rear steering, at least on the listed cars above
    ret.steerRatioRear = 0.
    ret.steerControlType = car.CarParams.SteerControlType.angle
    
    #Imported through tesla  no max steer limit VS speed
    ret.steerMaxBP = [0.,15.]  # m/s
    ret.steerMaxV = [420.,420.]   # max steer allowed

    # steer, gas, brake limitations VS speed
    #ret.steerMaxBP = [16. * CV.KPH_TO_MS, 45. * CV.KPH_TO_MS]  # breakpoints at 1 and 40 kph
    #ret.steerMaxV = [1., 1.]  # 2/3rd torque allowed above 45 kph
    ret.gasMaxBP = [0.]
    ret.gasMaxV = [0.5]
    ret.brakeMaxBP = [5., 20.]
    ret.brakeMaxV = [1., 0.8]

    ret.enableCamera = not check_ecu_msgs(fingerprint, ECU.CAM)
    ret.enableDsu = not check_ecu_msgs(fingerprint, ECU.DSU)
    ret.enableApgs = False #not check_ecu_msgs(fingerprint, ECU.APGS)
    ret.openpilotLongitudinalControl = ret.enableCamera and ret.enableDsu
    cloudlog.warn("ECU Camera Simulated: %r", ret.enableCamera)
    cloudlog.warn("ECU DSU Simulated: %r", ret.enableDsu)
    cloudlog.warn("ECU APGS Simulated: %r", ret.enableApgs)

    ret.steerLimitAlert = False
    ret.stoppingControl = False
    ret.startAccel = 0.0

    ret.longitudinalKpBP = [0., 5., 35.]
    ret.longitudinalKpV = [3.6, 2.4, 1.5]
    ret.longitudinalKiBP = [0., 35.]
    ret.longitudinalKiV = [0.54, 0.36]

    return ret

  # returns a car.CarState
  def update(self, c):
    # ******************* do can recv *******************
    canMonoTimes = []

    self.cp.update(int(sec_since_boot() * 1e9), False)

    self.CS.update(self.cp)

    # create message
    ret = car.CarState.new_message()

    # speeds
    ret.vEgo = self.CS.v_ego
    ret.vEgoRaw = self.CS.v_ego_raw
    ret.aEgo = self.CS.a_ego
    ret.yawRate = self.VM.yaw_rate(self.CS.angle_steers * CV.DEG_TO_RAD, self.CS.v_ego)
    ret.standstill = self.CS.standstill
    ret.wheelSpeeds.fl = self.CS.v_wheel_fl
    ret.wheelSpeeds.fr = self.CS.v_wheel_fr
    ret.wheelSpeeds.rl = self.CS.v_wheel_rl
    ret.wheelSpeeds.rr = self.CS.v_wheel_rr

    # gear shifter
    ret.gearShifter = self.CS.gear_shifter

    # gas pedal
    ret.gas = self.CS.car_gas
    ret.gasPressed = self.CS.pedal_gas > 0

    # brake pedal
    ret.brake = self.CS.user_brake
    ret.brakePressed = self.CS.brake_pressed != 0
    ret.brakeLights = self.CS.brake_lights

    # steering wheel
    ret.steeringAngle = self.CS.angle_steers
    ret.steeringRate = self.CS.angle_steers_rate

    ret.steeringTorque = self.CS.steer_torque_driver
    ret.steeringPressed = self.CS.steer_override
    
    # Double Stalk Pull Logic
    # 2 taps of on/off will enable/disable
    if (self.CS.cruise_stalk_pull != self.last_cruise_stalk_pull):
      self.cruise_stalk_pull_time = (sec_since_boot() * 1e3)
      if ((self.cruise_stalk_pull_time - self.last_cruise_stalk_pull_time) < 500):
        #Stalk pulled twice, enable or disable
        if (self.user_enabled == True):
          self.user_enabled = False
        else:
          self.user_enabled = True
          self.CS.enabled_time = (sec_since_boot() * 1e3)
      self.last_cruise_stalk_pull_time = self.cruise_stalk_pull_time
    self.last_cruise_stalk_pull = self.CS.cruise_stalk_pull
    
    # for safety loop
    self.current_time = (sec_since_boot() * 1e3)
    
    #safety CAN check
    if (self.can_check != self.CS.can_check):
      self.last_can_check_time = (sec_since_boot() * 1e3)
      self.can_check = self.CS.can_check

    # cruise state
    ret.cruiseState.enabled = self.user_enabled #self.CS.pcm_acc_status != 0
    ret.cruiseState.speed = self.CS.v_cruise_pcm * CV.KPH_TO_MS
    ret.cruiseState.available = bool(self.CS.main_on)
    ret.cruiseState.speedOffset = 0.
    if self.CP.carFingerprint in [CAR.CAMRYH]:
      # ignore standstill in hybrid vehicles, since pcm allows to restart without
      # receiving any special command
      ret.cruiseState.standstill = False
    else:
      ret.cruiseState.standstill = self.CS.pcm_acc_status == 7

    buttonEvents = []
    if self.CS.left_blinker_on != self.CS.prev_left_blinker_on:
      be = car.CarState.ButtonEvent.new_message()
      be.type = 'leftBlinker'
      be.pressed = self.CS.left_blinker_on != 0
      buttonEvents.append(be)

    if self.CS.right_blinker_on != self.CS.prev_right_blinker_on:
      be = car.CarState.ButtonEvent.new_message()
      be.type = 'rightBlinker'
      be.pressed = self.CS.right_blinker_on != 0
      buttonEvents.append(be)

    ret.buttonEvents = buttonEvents
    ret.leftBlinker = bool(self.CS.left_blinker_on)
    ret.rightBlinker = bool(self.CS.right_blinker_on)

    ret.doorOpen = not self.CS.door_all_closed
    ret.seatbeltUnlatched = not self.CS.seatbelt

    ret.genericToggle = self.CS.generic_toggle

    # events
    events = []
    if not self.CS.can_valid:
      self.can_invalid_count += 1
      if self.can_invalid_count >= 5:
        events.append(create_event('commIssue', [ET.NO_ENTRY, ET.IMMEDIATE_DISABLE]))
    else:
      self.can_invalid_count = 0
      #Gear CAN command not known for camry
    #if not ret.gearShifter == 'drive' and self.CP.enableDsu:
      #events.append(create_event('wrongGear', [ET.NO_ENTRY, ET.SOFT_DISABLE]))
    if ret.doorOpen:
      events.append(create_event('doorOpen', [ET.NO_ENTRY, ET.SOFT_DISABLE]))
    if ret.seatbeltUnlatched:
      events.append(create_event('seatbeltNotLatched', [ET.NO_ENTRY, ET.SOFT_DISABLE]))
    if self.CS.esp_disabled and self.CP.enableDsu:
      events.append(create_event('espDisabled', [ET.NO_ENTRY, ET.SOFT_DISABLE]))
    if not self.CS.main_on and self.CP.enableDsu:
      events.append(create_event('wrongCarMode', [ET.NO_ENTRY, ET.USER_DISABLE]))
    #if ret.gearShifter == 'reverse' and self.CP.enableDsu:
      #events.append(create_event('reverseGear', [ET.NO_ENTRY, ET.IMMEDIATE_DISABLE]))
    if self.CS.steer_error:
      events.append(create_event('steerTempUnavailable', [ET.NO_ENTRY, ET.WARNING]))
    if self.CS.low_speed_lockout and self.CP.enableDsu:
      events.append(create_event('lowSpeedLockout', [ET.NO_ENTRY, ET.PERMANENT]))
    if ret.vEgo < self.CP.minEnableSpeed and self.CP.enableDsu:
      events.append(create_event('speedTooLow', [ET.NO_ENTRY]))
      if c.actuators.gas > 0.1:
        # some margin on the actuator to not false trigger cancellation while stopping
        events.append(create_event('speedTooLow', [ET.IMMEDIATE_DISABLE]))
      if ret.vEgo < 0.001:
        # while in standstill, send a user alert
        events.append(create_event('manualRestart', [ET.WARNING]))

    # enable request in prius is simple, as we activate when Toyota is active (rising edge)
    if ret.cruiseState.enabled and not self.cruise_enabled_prev:
      events.append(create_event('pcmEnable', [ET.ENABLE]))
    elif not ret.cruiseState.enabled:
      events.append(create_event('pcmDisable', [ET.USER_DISABLE]))

    # disable on pedals rising edge or when brake is pressed and speed isn't zero
    if (ret.gasPressed and not self.gas_pressed_prev) or \
       (ret.brakePressed and (not self.brake_pressed_prev or ret.vEgo > 0.001)):
      events.append(create_event('pedalPressed', [ET.NO_ENTRY, ET.USER_DISABLE]))
      self.user_enabled = False

    if ret.gasPressed:
      events.append(create_event('pedalPressed', [ET.PRE_ENABLE]))
       
    #Disable if started for over 3 seconds and delta angle >5 degrees
    if (abs(self.CS.desired_angle - self.CS.angle_steers) > 5) and \
       ((self.current_time - self.CS.enabled_time) > 3000) and (self.user_enabled):
      # disable if angle not moving towards desired angle
      if (self.CS.desired_angle > self.CS.angle_steers):
        if not (self.CS.angle_steers > (self.last_angle_steers - 0.2)):
          self.user_enabled = False
          events.append(create_event('motorIssue', [ET.IMMEDIATE_DISABLE]))
      # disable if angle not moving towards desired angle
      elif (self.CS.desired_angle < self.CS.angle_steers):
        if not (self.CS.angle_steers < (self.last_angle_steers + 0.2)):
          self.user_enabled = False
          events.append(create_event('motorIssue', [ET.IMMEDIATE_DISABLE]))
          
    #Disable if haven't heard from arduino in 0.5 seconds
    if ((self.current_time - self.last_can_check_time) > 500):
      events.append(create_event('steerCANerror', [ET.IMMEDIATE_DISABLE]))
      
    ret.events = events
    ret.canMonoTimes = canMonoTimes

    self.gas_pressed_prev = ret.gasPressed
    self.brake_pressed_prev = ret.brakePressed
    self.cruise_enabled_prev = ret.cruiseState.enabled
    self.last_angle_steers = self.CS.angle_steers
   
     

    return ret.as_reader()

  # pass in a car.CarControl
  # to be called @ 100hz
  def apply(self, c, perception_state=log.Live20Data.new_message()):

    self.CC.update(self.sendcan, c.enabled, self.CS, self.frame,
                   c.actuators, c.cruiseControl.cancel, c.hudControl.visualAlert,
                   c.hudControl.audibleAlert)

    self.frame += 1
    return False
