#!/usr/bin/env python3
import os
import numpy as np
from cereal import car
from openpilot.common.params import Params
from openpilot.common.realtime import Priority, config_realtime_process
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.modeld.constants import ModelConstants
from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner
from openpilot.selfdrive.controls.lib.lateral_planner import LateralPlanner
import cereal.messaging as messaging

from openpilot.selfdrive.frogpilot.functions.frogpilot_planner import FrogPilotPlanner

def cumtrapz(x, t):
  return np.concatenate([[0], np.cumsum(((x[0:-1] + x[1:])/2) * np.diff(t))])

def publish_ui_plan(sm, pm, lateral_planner, longitudinal_planner):
  plan_odo = cumtrapz(longitudinal_planner.v_desired_trajectory_full, ModelConstants.T_IDXS)
  model_odo = cumtrapz(lateral_planner.v_plan, ModelConstants.T_IDXS)

  ui_send = messaging.new_message('uiPlan')
  ui_send.valid = sm.all_checks(service_list=['carState', 'controlsState', 'modelV2'])
  uiPlan = ui_send.uiPlan
  uiPlan.frameId = sm['modelV2'].frameId
  uiPlan.position.x = np.interp(plan_odo, model_odo, lateral_planner.x_sol[:,0]).tolist()
  uiPlan.position.y = np.interp(plan_odo, model_odo, lateral_planner.x_sol[:,1]).tolist()
  uiPlan.position.z = np.interp(plan_odo, model_odo, lateral_planner.path_xyz[:,2]).tolist()
  uiPlan.accel = longitudinal_planner.a_desired_trajectory_full.tolist()
  pm.send('uiPlan', ui_send)

def plannerd_thread():
  config_realtime_process(5, Priority.CTRL_LOW)

  cloudlog.info("plannerd is waiting for CarParams")
  params = Params()
  params_memory = Params("/dev/shm/params")
  with car.CarParams.from_bytes(params.get("CarParams", block=True)) as msg:
    CP = msg
  cloudlog.info("plannerd got CarParams: %s", CP.carName)

  debug_mode = bool(int(os.getenv("DEBUG", "0")))

  frogpilot_planner = FrogPilotPlanner(params)
  longitudinal_planner = LongitudinalPlanner(CP)
  lateral_planner = LateralPlanner(CP, debug=debug_mode)

  pm = messaging.PubMaster(['longitudinalPlan', 'lateralPlan', 'uiPlan', 'frogpilotLateralPlan', 'frogpilotLongitudinalPlan'])
  sm = messaging.SubMaster(['carControl', 'carState', 'controlsState', 'radarState', 'modelV2', 'frogpilotNavigation'],
                           poll=['radarState', 'modelV2'], ignore_avg_freq=['radarState'])

  while True:
    sm.update()

    if sm.updated['modelV2']:
      lateral_planner.update(sm, frogpilot_planner)
      lateral_planner.publish(sm, pm, frogpilot_planner)
      longitudinal_planner.update(sm, frogpilot_planner)
      longitudinal_planner.publish(sm, pm, frogpilot_planner)
      publish_ui_plan(sm, pm, lateral_planner, longitudinal_planner)

    if params_memory.get_bool("FrogPilotTogglesUpdated"):
      frogpilot_planner.update_frogpilot_params(params)

def main():
  plannerd_thread()


if __name__ == "__main__":
  main()
