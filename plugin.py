#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Author: ErwanBCN / RONELABS
# Version: 2.0.0

"""
<plugin key="ZZ-AIS7Z" name="RONELABS - Auto Irrigation Sys" author="ErwanBCN" version="2.0.0" externallink="https://ronelabs.com">
    <description>
        <h2>Automatic Irrigation System V2.0.0</h2><br/>
        Gestion automatique de 7 zones d'arrosage + 1 vanne générale.<br/>
        V2 nettoyée : démarrage sécurisé Zigbee, Auto/Test/Manuel, Info texte, UserVariable.
    </description>
    <params>
        <param field="Mode1" label="General Valve idx (ou CSV si plusieurs)" width="260px" required="true" default=""/>
        <param field="Mode2" label="7 Zone Valve idx CSV: Z1,Z2,Z3,Z4,Z5,Z6,Z7" width="420px" required="true" default=""/>
        <param field="Mode3" label="Starting hour ex 05:00" width="180px" required="true" default="05:00"/>
        <param field="Mode4" label="Zone timers minutes CSV: Z1,Z2,Z3,Z4,Z5,Z6,Z7" width="420px" required="true" default="5,5,10,10,8,8,5"/>
        <param field="Mode6" label="Logging Level" width="200px">
            <options>
                <option label="Normal" value="0" default="true"/>
                <option label="Debug - Python Only" value="2"/>
                <option label="Debug - Basic" value="62"/>
                <option label="Debug - All" value="1"/>
            </options>
        </param>
    </params>
</plugin>
"""

import json
import urllib.error
import urllib.parse as parse
import urllib.request as request
from datetime import datetime, timedelta
import Domoticz

try:
    from Domoticz import Devices, Parameters
except ImportError:
    pass


UNIT_CONTROL = 1
UNIT_MANUAL_ZONE = 2
UNIT_INFO = 3

MODE_OFF = 0
MODE_AUTO = 10
MODE_TEST = 20
MODE_MANUAL = 30

MANUAL_STOP_LEVEL = 0
MANUAL_TIMEOUT_SECONDS = 60

STARTUP_SAFETY_SECONDS = 60

USERVAR_LAST_MODE = "Irrigation_LastStableMode"


class BasePlugin:
    def __init__(self):
        self.debug = False

        self.main_valve_idxs = []
        self.zone_idxs = []
        self.start_time = "05:00"
        self.zone_minutes = [5, 5, 10, 10, 8, 8, 5]

        self.mode = MODE_OFF
        self.run_active = False
        self.run_type = None
        self.current_zone = 0
        self.current_zone_index = None
        self.zone_end_time = None
        self.last_auto_date = None
        self.manual_mode_deadline = None

        self.startup_phase = False
        self.startup_end = None

        self._device_state_cache = {}

    def onStart(self):
        Domoticz.Log("RONELABS Irrigation V2.0.0: onStart called")

        self._setup_debug()
        self._read_parameters()
        self._create_devices()

        Domoticz.Heartbeat(20)

        self._ensure_last_mode_uservariable()
        self._restore_mode_from_device()

        if self.mode in (MODE_TEST, MODE_MANUAL):
            self._restore_last_stable_mode(update_info=False)

        self._set_manual_selector_stop()

        self.startup_phase = True
        self.startup_end = datetime.now() + timedelta(seconds=STARTUP_SAFETY_SECONDS)
        self.run_active = False
        self.run_type = None
        self.zone_end_time = None
        self.manual_mode_deadline = None
        self._device_state_cache = {}

        Domoticz.Log(f"Startup safety phase started for {STARTUP_SAFETY_SECONDS} seconds")
        self._force_all_valves_off(reason="startup")

        self._update_info()
        Domoticz.Log(f"RONELABS Irrigation loaded. Mode={self.mode}")

    def onStop(self):
        Domoticz.Log("RONELABS Irrigation: onStop called")
        self._force_all_valves_off(reason="plugin stop")
        Domoticz.Debugging(0)

    def onCommand(self, Unit, Command, Level, Color):
        Domoticz.Log(f"RONELABS Irrigation: onCommand Unit={Unit} Command={Command} Level={Level}")

        self._ensure_last_mode_uservariable()

        if Unit == UNIT_CONTROL:
            fallback = Devices[UNIT_CONTROL].sValue if UNIT_CONTROL in Devices else MODE_OFF
            level = self._safe_level(Level, fallback)

            if self.startup_phase and level in (MODE_TEST, MODE_MANUAL):
                Domoticz.Log("Control command ignored during startup safety phase")
                self._restore_last_stable_mode(update_info=True)
                self._force_all_valves_off(reason="startup command ignored")
                return

            self._handle_control_command(level)
            self._update_info()
            return

        if Unit == UNIT_MANUAL_ZONE:
            fallback = Devices[UNIT_MANUAL_ZONE].sValue if UNIT_MANUAL_ZONE in Devices else MANUAL_STOP_LEVEL
            level = self._safe_level(Level, fallback)

            if self.startup_phase:
                Domoticz.Log("Manual selector ignored during startup safety phase")
                self._force_all_valves_off(reason="startup manual ignored")
                self._set_manual_selector_stop()
                self._update_info()
                return

            current_mode = self._safe_level(
                Devices[UNIT_CONTROL].sValue if UNIT_CONTROL in Devices else self.mode,
                self.mode
            )

            if current_mode != MODE_MANUAL:
                Domoticz.Log("Manual selector ignored because Control is not Manual")
                self._all_valves_off()
                self._set_manual_selector_stop()
                self._update_info()
                return

            self.mode = MODE_MANUAL
            self._handle_manual_zone_command(level)
            self._update_info()
            return

    def onHeartbeat(self):
        now = datetime.now()

        self._ensure_last_mode_uservariable()

        if self.startup_phase:
            self._force_all_valves_off(reason="startup heartbeat")
            self._set_manual_selector_stop()

            if self.debug:
                Domoticz.Debug(f"Startup safety active until {self.startup_end}")

            if now >= self.startup_end:
                Domoticz.Log("Startup safety phase finished")
                self._force_all_valves_off(reason="startup final")
                self._device_state_cache = {}
                self._mark_all_valves_cached_off()
                self.startup_phase = False

                self._restore_mode_from_device()
                if self.mode in (MODE_TEST, MODE_MANUAL):
                    self._restore_last_stable_mode(update_info=False)
                self._set_manual_selector_stop()

            self._update_info()
            return

        if self.mode != MODE_MANUAL:
            if UNIT_MANUAL_ZONE in Devices and Devices[UNIT_MANUAL_ZONE].sValue != str(MANUAL_STOP_LEVEL):
                self._set_manual_selector_stop()

        if self.debug:
            Domoticz.Debug(
                f"Heartbeat mode={self.mode} active={self.run_active} "
                f"type={self.run_type} current_zone={self.current_zone} "
                f"zone_index={self.current_zone_index} end={self.zone_end_time}"
            )

        if self.mode == MODE_AUTO and not self.run_active:
            if self._time_window_reached(now) and self.last_auto_date != now.date():
                self.start_sequence("auto")
                self.last_auto_date = now.date()

        if self.run_active and self.zone_end_time and now >= self.zone_end_time:
            if self.run_type in ("auto", "test"):
                self._advance_sequence(now)
            elif self.run_type == "manual":
                self.stop_sequence(reset_manual_selector=True)
                self._restore_last_stable_mode(update_info=False)

        if self.mode == MODE_MANUAL and not self.run_active and self.manual_mode_deadline:
            if now >= self.manual_mode_deadline:
                Domoticz.Log("Manual mode timeout: restoring previous stable mode")
                self.manual_mode_deadline = None
                self._all_valves_off()
                self._restore_last_stable_mode(update_info=False)

        self._update_info()

    def _handle_control_command(self, level):
        if level not in (MODE_OFF, MODE_AUTO, MODE_TEST, MODE_MANUAL):
            level = MODE_OFF

        previous_mode = self.mode

        if level == MODE_OFF:
            self.mode = MODE_OFF
            self.manual_mode_deadline = None
            self._write_last_stable_mode(MODE_OFF)
            self._update_selector(UNIT_CONTROL, MODE_OFF)
            self.stop_sequence(reset_manual_selector=True)
            Domoticz.Log("Irrigation mode: Off")
            return

        if level == MODE_AUTO:
            self.mode = MODE_AUTO
            self.manual_mode_deadline = None
            self._write_last_stable_mode(MODE_AUTO)
            self._update_selector(UNIT_CONTROL, MODE_AUTO)

            if self.run_active:
                self.stop_sequence(reset_manual_selector=True)
            else:
                self._set_manual_selector_stop()

            Domoticz.Log("Irrigation mode: Auto")
            return

        if level == MODE_TEST:
            if previous_mode in (MODE_OFF, MODE_AUTO):
                self._write_last_stable_mode(previous_mode)

            self.mode = MODE_TEST
            self.manual_mode_deadline = None
            self._update_selector(UNIT_CONTROL, MODE_TEST)
            self._set_manual_selector_stop()

            Domoticz.Log("Irrigation mode: Test - starting 1 minute per zone")
            self.start_sequence("test")
            return

        if level == MODE_MANUAL:
            if previous_mode in (MODE_OFF, MODE_AUTO):
                self._write_last_stable_mode(previous_mode)

            self.mode = MODE_MANUAL
            self._update_selector(UNIT_CONTROL, MODE_MANUAL)

            self.stop_sequence(reset_manual_selector=False, quiet=True)
            self._set_manual_selector_stop()
            self.manual_mode_deadline = datetime.now() + timedelta(seconds=MANUAL_TIMEOUT_SECONDS)

            Domoticz.Log("Irrigation mode: Manual - select zone within 1 minute")
            return

    def _handle_manual_zone_command(self, level):
        if self.mode != MODE_MANUAL:
            self._all_valves_off()
            self._set_manual_selector_stop()
            return

        if level == MANUAL_STOP_LEVEL:
            self.stop_sequence(reset_manual_selector=True)
            self._restore_last_stable_mode(update_info=False)
            return

        manual_level_to_zone_index = {
            10: 0,
            20: 1,
            30: 2,
            40: 3,
            50: 4,
            60: 5,
            70: 6,
        }

        if level not in manual_level_to_zone_index:
            Domoticz.Error(f"Invalid manual selector level: {level}")
            self._all_valves_off()
            self._set_manual_selector_stop()
            return

        zone_index = manual_level_to_zone_index[level]
        zone_number = zone_index + 1

        self.manual_mode_deadline = None
        self.start_manual_zone(zone_index, zone_number)

    def start_sequence(self, run_type):
        if len(self.zone_idxs) != 7:
            Domoticz.Error("Cannot start irrigation: Mode2 must contain exactly 7 zone idx")
            return

        if not self.main_valve_idxs:
            Domoticz.Error("Cannot start irrigation: Mode1 general valve idx missing")
            return

        self._close_zones_only()

        self.run_active = True
        self.run_type = run_type
        self.current_zone_index = 0
        self.current_zone = 1
        self.zone_end_time = None

        self._start_current_zone(datetime.now())

    def start_manual_zone(self, zone_index, zone_number):
        if zone_index < 0 or zone_index > 6 or len(self.zone_idxs) != 7:
            Domoticz.Error(f"Invalid manual zone index: {zone_index}")
            self._all_valves_off()
            self._set_manual_selector_stop()
            return

        self.run_active = True
        self.run_type = "manual"
        self.current_zone_index = zone_index
        self.current_zone = zone_number
        self.zone_end_time = datetime.now() + timedelta(minutes=1)

        self._open_only_zone(zone_index)
        self._update_selector(UNIT_MANUAL_ZONE, zone_number * 10)

        Domoticz.Log(
            f"Manual irrigation: Zone {zone_number} / idx={self.zone_idxs[zone_index]} On for max 1 minute"
        )

    def _start_current_zone(self, now):
        if self.current_zone_index is None:
            self.current_zone_index = 0

        if self.current_zone_index < 0 or self.current_zone_index > 6:
            Domoticz.Error(f"Invalid current_zone_index: {self.current_zone_index}")
            self.stop_sequence(reset_manual_selector=True)
            return

        duration = 1 if self.run_type == "test" else self.zone_minutes[self.current_zone_index]
        duration = max(0, float(duration))

        if duration <= 0:
            Domoticz.Log(f"Skipping zone {self.current_zone_index + 1}: duration is 0")
            self._advance_sequence(now)
            return

        self.current_zone = self.current_zone_index + 1
        self.zone_end_time = now + timedelta(minutes=duration)

        self._open_only_zone(self.current_zone_index)

        Domoticz.Log(
            f"Irrigation {self.run_type}: Zone {self.current_zone} / idx={self.zone_idxs[self.current_zone_index]} "
            f"On for {duration:g} minute(s)"
        )

    def _advance_sequence(self, now):
        if self.current_zone_index is None:
            self.current_zone_index = 0
        else:
            self.current_zone_index += 1

        if self.current_zone_index >= 7:
            finished_type = self.run_type
            self.stop_sequence(reset_manual_selector=True)

            if finished_type == "test":
                self._restore_last_stable_mode(update_info=False)
                Domoticz.Log("Test finished. Restored previous stable mode.")
            else:
                Domoticz.Log(f"Irrigation {finished_type or ''}: sequence finished")

            return

        self._start_current_zone(now)

    def stop_sequence(self, reset_manual_selector=True, quiet=False):
        self.run_active = False
        self.run_type = None
        self.current_zone = 0
        self.current_zone_index = None
        self.zone_end_time = None
        self.manual_mode_deadline = None

        self._all_valves_off()

        if reset_manual_selector:
            self._set_manual_selector_stop()

        if not quiet:
            Domoticz.Log("Irrigation stopped: all valves Off")

    def _open_only_zone(self, zone_index):
        if len(self.zone_idxs) != 7:
            Domoticz.Error("Cannot open zone: Mode2 must contain 7 idx")
            return

        target_idx = self.zone_idxs[zone_index]

        for idx in self.main_valve_idxs:
            self._switch_idx_if_needed(idx, "On")

        self._switch_idx_if_needed(target_idx, "On")

        for idx in self.zone_idxs:
            if idx != target_idx:
                self._switch_idx_if_needed(idx, "Off")

    def _close_zones_only(self):
        for idx in self.zone_idxs:
            self._switch_idx_if_needed(idx, "Off")

    def _all_valves_off(self):
        for idx in self.zone_idxs:
            self._switch_idx_if_needed(idx, "Off")

        for idx in self.main_valve_idxs:
            self._switch_idx_if_needed(idx, "Off")

    def _force_all_valves_off(self, reason="force off"):
        for idx in self.zone_idxs:
            self._switch_idx_force(idx, "Off", reason)

        for idx in self.main_valve_idxs:
            self._switch_idx_force(idx, "Off", reason)

    def _mark_all_valves_cached_off(self):
        for idx in self.zone_idxs + self.main_valve_idxs:
            if idx:
                self._device_state_cache[idx] = "Off"

    def _switch_idx_force(self, idx, command, reason="force"):
        if not idx:
            return False

        res = DomoticzAPI(f"type=command&param=switchlight&idx={idx}&switchcmd={command}")

        if not res or str(res.get("status", "")).lower() != "ok":
            Domoticz.Error(f"Force valve command failure idx={idx} command={command} reason={reason}")
            return False

        self._device_state_cache[idx] = command
        Domoticz.Log(f"{reason}: valve idx={idx} forced {command}")
        return True

    def _switch_idx_if_needed(self, idx, command):
        if not idx:
            return False

        current = self._device_state_cache.get(idx)

        if current is None:
            current = self._read_switch_state_from_domoticz(idx)
            if current in ("On", "Off"):
                self._device_state_cache[idx] = current

        if current == command:
            if self.debug:
                Domoticz.Debug(f"Valve idx={idx} already {command}, no command sent")
            return True

        res = DomoticzAPI(f"type=command&param=switchlight&idx={idx}&switchcmd={command}")

        if not res or str(res.get("status", "")).lower() != "ok":
            Domoticz.Error(f"Valve command failure idx={idx} command={command}")
            return False

        self._device_state_cache[idx] = command

        if self.debug:
            Domoticz.Debug(f"Valve idx={idx} => {command}")

        return True

    def _read_switch_state_from_domoticz(self, idx):
        res = DomoticzAPI(f"type=command&param=getdevices&rid={idx}")

        try:
            dev = res["result"][0]
        except Exception:
            return None

        status = str(dev.get("Status", "")).strip()
        data = str(dev.get("Data", "")).strip()

        if status in ("On", "Off"):
            return status

        if data in ("On", "Off"):
            return data

        try:
            nvalue = int(dev.get("nValue", 0))
            return "On" if nvalue == 1 else "Off"
        except Exception:
            return None

    def _update_info(self):
        if UNIT_INFO not in Devices:
            return

        now = datetime.now()

        if self.startup_phase:
            remaining = self._remaining_minutes(self.startup_end, now)
            text = f"STARTUP SAFETY - forcing all valves Off - Rem. {remaining} min"

        elif self.mode == MODE_OFF:
            text = "OFF"

        elif self.mode == MODE_AUTO and not self.run_active:
            text = f"AUTO - Next cycle at {self.start_time}"

        elif self.run_active:
            rem_zone = self._remaining_minutes(self.zone_end_time, now)
            rem_total = self._remaining_total_minutes(now)
            prefix = str(self.run_type).upper()
            text = f"{prefix} - ON Zone {self.current_zone} - Rem. {rem_zone} min - Total Rem. {rem_total} min"

        elif self.mode == MODE_TEST:
            text = "TEST - Waiting"

        elif self.mode == MODE_MANUAL:
            text = "MANUAL - Select zone"

        else:
            text = "UNKNOWN"

        self._update_text(UNIT_INFO, text)

    def _remaining_minutes(self, end_time, now=None):
        if not end_time:
            return 0

        if now is None:
            now = datetime.now()

        seconds = max(0, (end_time - now).total_seconds())
        return int((seconds + 59) // 60)

    def _remaining_total_minutes(self, now=None):
        if now is None:
            now = datetime.now()

        if not self.run_active:
            return 0

        total = self._remaining_minutes(self.zone_end_time, now)

        if self.run_type == "manual":
            return total

        if self.run_type == "test":
            if self.current_zone_index is not None:
                total += max(0, 6 - self.current_zone_index)
            return int(total)

        if self.run_type == "auto":
            if self.current_zone_index is not None:
                for i in range(self.current_zone_index + 1, 7):
                    total += max(0, float(self.zone_minutes[i]))
            return int((total + 0.9999))

        return total

    def _update_text(self, unit, text):
        if unit not in Devices:
            return

        text = str(text)

        if Devices[unit].sValue != text:
            Devices[unit].Update(nValue=0, sValue=text)

    def _ensure_last_mode_uservariable(self):
        existing = self._get_uservariable(USERVAR_LAST_MODE)

        if existing is not None:
            return True

        Domoticz.Log(f"UserVariable missing: creating {USERVAR_LAST_MODE}=0")

        res = DomoticzAPI(
            f"type=command&param=adduservariable"
            f"&vname={USERVAR_LAST_MODE}"
            f"&vtype=2"
            f"&vvalue=0"
        )

        if not res:
            Domoticz.Error(f"Failed to create UserVariable {USERVAR_LAST_MODE}")
            return False

        check = self._get_uservariable(USERVAR_LAST_MODE)

        if check is None:
            Domoticz.Error(f"UserVariable {USERVAR_LAST_MODE} still missing after creation")
            return False

        Domoticz.Log(f"Created UserVariable OK: {USERVAR_LAST_MODE}={check}")
        return True

    def _get_uservariable(self, name):
        res = DomoticzAPI("type=command&param=getuservariables")

        if not res:
            return None

        for item in res.get("result", []):
            if str(item.get("Name", "")) == name:
                return str(item.get("Value", ""))

        return None

    def _write_last_stable_mode(self, mode):
        if mode not in (MODE_OFF, MODE_AUTO):
            mode = MODE_OFF

        self._ensure_last_mode_uservariable()

        res = DomoticzAPI(
            f"type=command&param=updateuservariable"
            f"&vname={USERVAR_LAST_MODE}"
            f"&vtype=2"
            f"&vvalue={mode}"
        )

        if not res:
            Domoticz.Error(f"Failed to update UserVariable {USERVAR_LAST_MODE}={mode}")
            return False

        Domoticz.Log(f"UserVariable updated: {USERVAR_LAST_MODE}={mode}")
        return True

    def _read_last_stable_mode(self, default=MODE_OFF):
        value = self._get_uservariable(USERVAR_LAST_MODE)

        try:
            mode = int(value)
        except Exception:
            mode = default

        return mode if mode in (MODE_OFF, MODE_AUTO) else default

    def _restore_last_stable_mode(self, update_info=True):
        last = self._read_last_stable_mode(default=MODE_OFF)

        self.mode = last
        self.manual_mode_deadline = None
        self._update_selector(UNIT_CONTROL, last)
        self._set_manual_selector_stop()

        if update_info:
            self._update_info()

        Domoticz.Log(f"Restored previous stable mode: {last}")

    def _setup_debug(self):
        try:
            debuglevel = int(Parameters.get("Mode6", "0"))
        except Exception:
            debuglevel = 0

        self.debug = debuglevel != 0
        Domoticz.Debugging(debuglevel if self.debug else 0)

        if self.debug:
            DumpConfigToLog()

    def _read_parameters(self):
        self.main_valve_idxs = parseCSV_to_ints(Parameters.get("Mode1", ""))
        self.zone_idxs = parseCSV_to_ints(Parameters.get("Mode2", ""))
        self.start_time = Parameters.get("Mode3", "05:00").strip() or "05:00"
        self.zone_minutes = parseCSV_to_floats(Parameters.get("Mode4", "5,5,10,10,8,8,5"))

        if len(self.zone_idxs) != 7:
            Domoticz.Error("Mode2 error: expected exactly 7 comma-separated idx values")

        if len(self.zone_minutes) != 7:
            Domoticz.Error("Mode4 error: expected exactly 7 comma-separated minute values. Default used.")
            self.zone_minutes = [5, 5, 10, 10, 8, 8, 5]

        if not self._valid_time(self.start_time):
            Domoticz.Error(f"Mode3 error: invalid starting hour '{self.start_time}'. Default 05:00 used.")
            self.start_time = "05:00"

        Domoticz.Log(
            f"Irrigation config: main={self.main_valve_idxs}, zones={self.zone_idxs}, "
            f"start={self.start_time}, minutes={self.zone_minutes}"
        )

    def _create_devices(self):
        if UNIT_CONTROL not in Devices:
            options = {
                "LevelActions": "||||",
                "LevelNames": "Off|Auto|Test|Manuel",
                "LevelOffHidden": "false",
                "SelectorStyle": "1",
            }

            Domoticz.Device(
                Unit=UNIT_CONTROL,
                Name="Irrigation Control",
                TypeName="Selector Switch",
                Switchtype=18,
                Image=9,
                Options=options,
                Used=1,
            ).Create()

        if UNIT_MANUAL_ZONE not in Devices:
            options = {
                "LevelActions": "|||||||",
                "LevelNames": "Stop|Zone 1|Zone 2|Zone 3|Zone 4|Zone 5|Zone 6|Zone 7",
                "LevelOffHidden": "false",
                "SelectorStyle": "1",
            }

            Domoticz.Device(
                Unit=UNIT_MANUAL_ZONE,
                Name="Manual Irrigation Zone",
                TypeName="Selector Switch",
                Switchtype=18,
                Image=9,
                Options=options,
                Used=1,
            ).Create()

        if UNIT_INFO not in Devices:
            Domoticz.Device(
                Unit=UNIT_INFO,
                Name="Irrigation Info",
                TypeName="Text",
                Used=1,
            ).Create()

    def _restore_mode_from_device(self):
        try:
            saved = int(Devices[UNIT_CONTROL].sValue)
        except Exception:
            saved = MODE_OFF

        if saved not in (MODE_OFF, MODE_AUTO, MODE_TEST, MODE_MANUAL):
            saved = MODE_OFF

        self.mode = saved

    def _set_manual_selector_stop(self):
        self._update_selector(UNIT_MANUAL_ZONE, MANUAL_STOP_LEVEL)

    def _time_window_reached(self, now):
        try:
            hour, minute = [int(x) for x in self.start_time.split(":")]
            scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            delta = (now - scheduled).total_seconds()
            return 0 <= delta < 60
        except Exception:
            return False

    def _valid_time(self, value):
        try:
            hour, minute = [int(x) for x in value.split(":")]
            return 0 <= hour <= 23 and 0 <= minute <= 59
        except Exception:
            return False

    def _safe_level(self, level, fallback):
        try:
            return int(level)
        except Exception:
            try:
                return int(fallback)
            except Exception:
                return 0

    def _update_selector(self, unit, level):
        if unit not in Devices:
            return

        nvalue = 0 if int(level) == 0 else 1
        svalue = str(int(level))

        if Devices[unit].nValue != nvalue or Devices[unit].sValue != svalue:
            Devices[unit].Update(nValue=nvalue, sValue=svalue)


def DomoticzAPI(APICall):
    resultJson = None
    url = f"http://127.0.0.1:8080/json.htm?{parse.quote(APICall, safe='&=')}"

    try:
        Domoticz.Debug(f"Domoticz API request: {url}")
        req = request.Request(url)
        response = request.urlopen(req, timeout=10)

        if response.status == 200:
            resultJson = json.loads(response.read().decode("utf-8"))

            if resultJson.get("status") == "ERR":
                Domoticz.Error(f"Domoticz API returned ERR for {APICall}: {resultJson}")
                return None
        else:
            Domoticz.Error(f"Domoticz API HTTP error = {response.status}")

    except urllib.error.HTTPError as e:
        Domoticz.Error(f"HTTP error calling '{url}': {e}")
    except urllib.error.URLError as e:
        Domoticz.Error(f"URL error calling '{url}': {e}")
    except json.JSONDecodeError as e:
        Domoticz.Error(f"JSON decoding error: {e}")
    except Exception as e:
        Domoticz.Error(f"Error calling '{url}': {e}")

    return resultJson


def parseCSV_to_ints(s):
    out = []

    for x in str(s).split(","):
        x = x.strip()

        if not x:
            continue

        try:
            out.append(int(x))
        except Exception:
            Domoticz.Error(f"Invalid integer in CSV: {x}")

    return out


def parseCSV_to_floats(s):
    out = []

    for x in str(s).split(","):
        x = x.strip()

        if not x:
            continue

        try:
            out.append(float(x))
        except Exception:
            Domoticz.Error(f"Invalid number in CSV: {x}")

    return out


def DumpConfigToLog():
    for x in Parameters:
        if Parameters[x] != "":
            Domoticz.Debug("'" + x + "':'" + str(Parameters[x]) + "'")

    Domoticz.Debug("Device count: " + str(len(Devices)))

    for x in Devices:
        Domoticz.Debug("Device:       " + str(x) + " - " + str(Devices[x]))
        Domoticz.Debug("Device ID:    '" + str(Devices[x].ID) + "'")
        Domoticz.Debug("Device Name:  '" + Devices[x].Name + "'")
        Domoticz.Debug("Device nValue: " + str(Devices[x].nValue))
        Domoticz.Debug("Device sValue:'" + Devices[x].sValue + "'")
        Domoticz.Debug("LastLevel:    " + str(Devices[x].LastLevel))


_global_plugin = BasePlugin()


def onStart():
    global _global_plugin
    _global_plugin.onStart()


def onStop():
    global _global_plugin
    _global_plugin.onStop()


def onCommand(Unit, Command, Level, Color):
    global _global_plugin
    _global_plugin.onCommand(Unit, Command, Level, Color)


def onHeartbeat():
    global _global_plugin
    _global_plugin.onHeartbeat()
