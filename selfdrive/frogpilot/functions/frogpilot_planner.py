import cereal.messaging as messaging
import numpy as np

from openpilot.common.conversions import Conversions as CV
from openpilot.common.numpy_fast import interp
from openpilot.selfdrive.controls.lib.drive_helpers import CONTROL_N, V_CRUISE_MAX
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import T_IDXS as T_IDXS_MPC
from openpilot.selfdrive.controls.lib.longitudinal_planner import A_CRUISE_MIN, A_CRUISE_MAX_VALS, A_CRUISE_MAX_BP, get_max_accel
from openpilot.selfdrive.modeld.constants import ModelConstants

from openpilot.selfdrive.frogpilot.functions.conditional_experimental_mode import ConditionalExperimentalMode

# Acceleration profiles - Credit goes to the DragonPilot team!
                 # MPH = [0.,  35,   35,  40,    40,  45,    45,  67,    67,   67, 123]
A_CRUISE_MIN_BP_CUSTOM = [0., 2.0, 2.01, 11., 11.01, 18., 18.01, 28., 28.01,  33., 55.]
                 # MPH = [0., 6.71, 13.4, 17.9, 24.6, 33.6, 44.7, 55.9, 67.1, 123]
A_CRUISE_MAX_BP_CUSTOM = [0.,    3,   6.,   8.,  11.,  15.,  20.,  25.,  30., 55.]

A_CRUISE_MIN_VALS_ECO_TUNE = [-0.480, -0.480, -0.40, -0.40, -0.40, -0.36, -0.32, -0.28, -0.28, -0.25, -0.25]
A_CRUISE_MAX_VALS_ECO_TUNE = [3.5, 3.3, 1.7, 1.1, .76, .62, .47, .36, .28, .09]

A_CRUISE_MIN_VALS_SPORT_TUNE = [-0.500, -0.500, -0.42, -0.42, -0.42, -0.42, -0.40, -0.35, -0.35, -0.30, -0.30]
A_CRUISE_MAX_VALS_SPORT_TUNE = [3.5, 3.5, 3.0, 2.6, 1.4, 1.0, 0.7, 0.6, .38, .2]

def get_min_accel_eco_tune(v_ego):
  return interp(v_ego, A_CRUISE_MIN_BP_CUSTOM, A_CRUISE_MIN_VALS_ECO_TUNE)

def get_max_accel_eco_tune(v_ego):
  return interp(v_ego, A_CRUISE_MAX_BP_CUSTOM, A_CRUISE_MAX_VALS_ECO_TUNE)

def get_min_accel_sport_tune(v_ego):
  return interp(v_ego, A_CRUISE_MIN_BP_CUSTOM, A_CRUISE_MIN_VALS_SPORT_TUNE)

def get_max_accel_sport_tune(v_ego):
  return interp(v_ego, A_CRUISE_MAX_BP_CUSTOM, A_CRUISE_MAX_VALS_SPORT_TUNE)


class FrogPilotPlanner:
  def __init__(self, params):
    self.cem = ConditionalExperimentalMode()

    self.v_cruise = 0

    self.x_desired_trajectory = np.zeros(CONTROL_N)

    self.update_frogpilot_params(params)

  def update(self, sm, mpc):
    carState, controlsState, modelData = sm['carState'], sm['controlsState'], sm['modelV2']

    enabled = controlsState.enabled

    v_cruise_kph = min(controlsState.vCruise, V_CRUISE_MAX)
    v_cruise = v_cruise_kph * CV.KPH_TO_MS
    v_ego = carState.vEgo

    # Acceleration profiles
    if self.acceleration_profile == 1:
      self.accel_limits = [get_min_accel_eco_tune(v_ego), get_max_accel_eco_tune(v_ego)]
    elif self.acceleration_profile in (2, 3):
      self.accel_limits = [get_min_accel_sport_tune(v_ego), get_max_accel_sport_tune(v_ego)]
    else:
      self.accel_limits = [A_CRUISE_MIN, get_max_accel(v_ego)]

    # Conditional Experimental Mode
    if self.conditional_experimental_mode and enabled:
      self.cem.update(carState, sm['frogpilotNavigation'], sm['modelV2'], mpc, sm['radarState'], carState.standstill, v_ego)

    self.v_cruise = self.update_v_cruise(carState, controlsState, modelData, enabled, v_cruise, v_ego)

    self.x_desired_trajectory_full = np.interp(ModelConstants.T_IDXS, T_IDXS_MPC, mpc.x_solution)
    self.x_desired_trajectory = self.x_desired_trajectory_full[:CONTROL_N]

  def update_v_cruise(self, carState, controlsState, modelData, enabled, v_cruise, v_ego):
    v_ego_diff = max(carState.vEgoRaw - carState.vEgoCluster, 0)
    return v_cruise - v_ego_diff

  def publish_lateral(self, sm, pm, DH):
    frogpilot_lateral_plan_send = messaging.new_message('frogpilotLateralPlan')
    frogpilot_lateral_plan_send.valid = sm.all_checks(service_list=['carState', 'controlsState', 'modelV2'])
    frogpilotLateralPlan = frogpilot_lateral_plan_send.frogpilotLateralPlan

    pm.send('frogpilotLateralPlan', frogpilot_lateral_plan_send)

  def publish_longitudinal(self, sm, pm, mpc):
    frogpilot_longitudinal_plan_send = messaging.new_message('frogpilotLongitudinalPlan')
    frogpilot_longitudinal_plan_send.valid = sm.all_checks(service_list=['carState', 'controlsState'])
    frogpilotLongitudinalPlan = frogpilot_longitudinal_plan_send.frogpilotLongitudinalPlan

    frogpilotLongitudinalPlan.conditionalExperimental = self.cem.experimental_mode
    frogpilotLongitudinalPlan.distances = self.x_desired_trajectory.tolist()

    pm.send('frogpilotLongitudinalPlan', frogpilot_longitudinal_plan_send)

  def update_frogpilot_params(self, params):
    self.is_metric = params.get_bool("IsMetric")

    self.conditional_experimental_mode = params.get_bool("ConditionalExperimental")
    if self.conditional_experimental_mode:
      self.cem.update_frogpilot_params(self.is_metric, params)
      if not params.get_bool("ExperimentalMode"):
        params.put_bool("ExperimentalMode", True)

    lateral_tune = params.get_bool("LateralTune")
    self.average_desired_curvature = params.get_bool("AverageCurvature") and lateral_tune

    longitudinal_tune = params.get_bool("LongitudinalTune")
    self.acceleration_profile = params.get_int("AccelerationProfile") if longitudinal_tune else 0
    self.aggressive_acceleration = params.get_bool("AggressiveAcceleration") and longitudinal_tune
