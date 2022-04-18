from cereal import car
from common.realtime import DT_CTRL
from common.numpy_fast import interp, clip
from common.conversions import Conversions as CV
from selfdrive.car import apply_std_steer_torque_limits, create_gas_interceptor_command
from selfdrive.car.gm import gmcan
from selfdrive.car.gm.values import DBC, CanBus, CarControllerParams
from opendbc.can.packer import CANPacker
from selfdrive.car.hyundai.scc_smoother import SccSmoother
min_set_speed = 30 * CV.KPH_TO_MS
VisualAlert = car.CarControl.HUDControl.VisualAlert
LongCtrlState = car.CarControl.Actuators.LongControlState
VEL = [13.889, 16.667, 25.]  # velocities
MIN_PEDAL = [0.02, 0.05, 0.1]

def accel_hysteresis(accel, accel_steady):
    # for small accel oscillations less than 0.02, don't change the accel command
    if accel > accel_steady + 0.02:
        accel_steady = accel - 0.02
    elif accel < accel_steady - 0.02:
        accel_steady = accel + 0.02
    accel = accel_steady

    return accel, accel_steady

def compute_gas_brake(accel, speed):
  creep_brake = 0.0
  creep_speed = 2.3
  creep_brake_value = 0.15
  if speed < creep_speed:
    creep_brake = (creep_speed - speed) / creep_speed * creep_brake_value
  gb = float(accel) / 4.0 - creep_brake
  return clip(gb, 0.0, 1.0), clip(-gb, 0.0, 1.0)

class CarController():
  def __init__(self, dbc_name, CP, VM):
    self.start_time = 0.
    self.apply_steer_last = 0
    self.apply_gas = 0
    self.apply_brake = 0
    self.lka_steering_cmd_counter_last = -1
    self.lka_icon_status_last = (False, False)
    self.steer_rate_limited = False
    
    self.accel_steady = 0.    
    self.params = CarControllerParams(CP)

    self.packer_pt = CANPacker(DBC[CP.carFingerprint]['pt'])
    #self.packer_obj = CANPacker(DBC[CP.carFingerprint]['radar'])
    #self.packer_ch = CANPacker(DBC[CP.carFingerprint]['chassis'])

    self.scc_smoother = SccSmoother.instance()
    self.frame = 0
    self.longcontrol = CP.openpilotLongitudinalControl
    self.packer = CANPacker(dbc_name)
    self.regenPaddleApplied = False


  def update(self,c,  enabled, CS, controls ,  actuators,
             hud_v_cruise, hud_show_lanes, hud_show_car, hud_alert):

    P = self.params
    self.regenPaddleApplied = False
    # Send CAN commands.
    can_sends = []

    # Steering (50Hz)
    # Avoid GM EPS faults when transmitting messages too close together: skip this transmit if we just received the
    # next Panda loopback confirmation in the current CS frame.
    if CS.lka_steering_cmd_counter != self.lka_steering_cmd_counter_last:
      self.lka_steering_cmd_counter_last = CS.lka_steering_cmd_counter
    elif (self.frame % P.STEER_STEP) == 0:
      lkas_enabled = enabled and not (CS.out.steerFaultTemporary or CS.out.steerFaultPermanent) and CS.out.vEgo > P.MIN_STEER_SPEED
      if lkas_enabled:
        new_steer = int(round(actuators.steer * P.STEER_MAX))
        apply_steer = apply_std_steer_torque_limits(new_steer, self.apply_steer_last, CS.out.steeringTorque, P)
        self.steer_rate_limited = new_steer != apply_steer
      else:
        apply_steer = 0

      self.apply_steer_last = apply_steer
      # GM EPS faults on any gap in received message counters. To handle transient OP/Panda safety sync issues at the
      # moment of disengaging, increment the counter based on the last message known to pass Panda safety checks.
      idx = (CS.lka_steering_cmd_counter + 1) % 4

      can_sends.append(gmcan.create_steering_control(self.packer_pt, CanBus.POWERTRAIN, apply_steer, idx, lkas_enabled))

    # Pedal/Regen
    comma_pedal =0  #for supress linter error.
#    accelMultiplier = 0.475 #default initializer.
#    if CS.out.vEgo * CV.MS_TO_KPH < 10 :
#      accelMultiplier = 0.400
#    elif CS.out.vEgo * CV.MS_TO_KPH < 40 :
#      accelMultiplier = 0.475
#    else : # above 40 km/h
#      accelMultiplier = 0.425

    if not enabled or not CS.adaptive_Cruise or not CS.CP.enableGasInterceptor:
      comma_pedal = 0
    elif CS.adaptive_Cruise:
      acc_mult = interp(CS.out.vEgo, [0., 5.], [0.17, 0.25])
      comma_pedal = clip(actuators.accel*acc_mult, 0., 1.)
            
      if actuators.accel < 0.10 and not self.regenPaddleApplied:
        can_sends.append(gmcan.create_regen_paddle_command(self.packer_pt, CanBus.POWERTRAIN))
        self.regenPaddleApplied = True

      if controls.LoC.pid.f < - 0.75  and not self.regenPaddleApplied:
        can_sends.append(gmcan.create_regen_paddle_command(self.packer_pt, CanBus.POWERTRAIN))
        self.regenPaddleApplied = True

    if (self.frame % 4) == 0:
      idx = (self.frame // 4) % 4
      can_sends.append(create_gas_interceptor_command(self.packer_pt, comma_pedal, idx))
      
    # Send dashboard UI commands (ACC status), 25hz
    #if (frame % 4) == 0:
    #  send_fcw = hud_alert == VisualAlert.fcw
    #  can_sends.append(gmcan.create_acc_dashboard_command(self.packer_pt, CanBus.POWERTRAIN, enabled, hud_v_cruise * CV.MS_TO_KPH, hud_show_car, send_fcw))

    # Radar needs to know current speed and yaw rate (50hz) - Delete
    # and that ADAS is alive (10hz)

    #if frame % P.ADAS_KEEPALIVE_STEP == 0:
    #  can_sends += gmcan.create_adas_keepalive(CanBus.POWERTRAIN)

    # Show green icon when LKA torque is applied, and
    # alarming orange icon when approaching torque limit.
    # If not sent again, LKA icon disappears in about 5 seconds.
    # Conveniently, sending camera message periodically also works as a keepalive.
    lka_active = CS.lkas_status == 1
    lka_critical = lka_active and abs(actuators.steer) > 0.9
    lka_icon_status = (lka_active, lka_critical)
    if self.frame % P.CAMERA_KEEPALIVE_STEP == 0 or lka_icon_status != self.lka_icon_status_last:
      steer_alert = hud_alert in [VisualAlert.steerRequired, VisualAlert.ldw]
      can_sends.append(gmcan.create_lka_icon_command(CanBus.SW_GMLAN, lka_active, lka_critical, steer_alert))
      self.lka_icon_status_last = lka_icon_status

    new_actuators = actuators.copy()
    new_actuators.steer = self.apply_steer_last / P.STEER_MAX
    new_actuators.gas = self.apply_gas
    new_actuators.brake = self.apply_brake

    self.update_scc(c, CS, actuators, controls, None, can_sends)
    self.frame += 1
    return new_actuators, can_sends


  def update_scc(self, CC, CS, actuators, controls, hud_control, can_sends):

    # scc smoother
    self.scc_smoother.update(CC.enabled, can_sends, self.packer, CC, CS, self.frame, controls)

    # send scc to car if longcontrol enabled and SCC not on bus 0 or ont live
    if self.longcontrol and CS.cruiseState_enabled :

      if self.frame % 2 == 0:


        stopping = controls.LoC.long_control_state == LongCtrlState.stopping
        apply_accel = clip(actuators.accel if CC.longActive else 0,
                           CarControllerParams.ACCEL_MIN, CarControllerParams.ACCEL_MAX)
        apply_accel = self.scc_smoother.get_apply_accel(CS, controls.sm, apply_accel, stopping)

        self.accel = apply_accel

        controls.apply_accel = apply_accel
        aReqValue = apply_accel
        controls.aReqValue = aReqValue

        if aReqValue < controls.aReqValueMin:
          controls.aReqValueMin = controls.aReqValue

        if aReqValue > controls.aReqValueMax:
          controls.aReqValueMax = controls.aReqValue


        controls.sccStockCamAct = 0
        controls.sccStockCamStatus = 0




