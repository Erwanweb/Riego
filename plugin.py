#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Author: ErwanBCN / RONELABS
# Version: 1.1.0

"""
<plugin key="ZZ-AIS7Z" name="RONELABS - Auto Irrigation Sys" author="ErwanBCN" version="1.1.0" externallink="https://ronelabs.com">
    <description>
        <h2>Automatic Irrigation System V1.1.0</h2><br/>
        Gestion automatique de 7 zones d'arrosage + 1 vanne générale.<br/>
        <ul>
            <li>Mode Off / Auto / Test</li>
            <li>Départ automatique à l'heure Mode3</li>
            <li>Durées par zone en Mode4</li>
            <li>Mode Test: 1 minute par zone</li>
            <li>Commande manuelle Zone 0 à 7, durée max 1 minute</li>
        </ul>
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


UNIT_MODE = 1
UNIT_MANUAL_ZONE = 2

MODE_OFF = 0
MODE_AUTO = 10
MODE_TEST = 20


class BasePlugin:
    def __init__(self):
        self.debug = False
        self.main_valve_idxs = []
        self.zone_idxs = []
        self.start_time = "05:00"
        self.zone_minutes = [5, 5, 10, 10, 8, 8, 5]

        self.mode = MODE_AUTO
        self.run_active = False
        self.run_type = None       # "auto", "test" ou "manual"
        self.current_zone = 0      # index 0..6 pour auto/test, ou zone 1..7 en manuel
        self.zone_end_time = None
        self.last_auto_date = None

    # ---------------------------------------------------------------------
    # Domoticz lifecycle
    # ---------------------------------------------------------------------

    def onStart(self):
        Domoticz.Log("RONELABS Irrigation: onStart called")
        self._setup_debug()
        self._read_parameters()
        self._create_devices()
        self._restore_mode_from_device()

        # Sécurité au démarrage: toutes les vannes fermées.
        self._all_valves_off()
        Domoticz.Heartbeat(5)

    def onStop(self):
        Domoticz.Log("RONELABS Irrigation: onStop called")
        self._all_valves_off()
        Domoticz.Debugging(0)

    def onCommand(self, Unit, Command, Level, Color):
        Domoticz.Log(f"RONELABS Irrigation: onCommand Unit={Unit} Command={Command} Level={Level}")

        if Unit == UNIT_MODE:
            level = self._safe_level(Level, Devices[UNIT_MODE].sValue if UNIT_MODE in Devices else MODE_AUTO)
            self._handle_mode_command(level)
            return

        if Unit == UNIT_MANUAL_ZONE:
            level = self._safe_level(Level, Devices[UNIT_MANUAL_ZONE].sValue if UNIT_MANUAL_ZONE in Devices else 0)
            self._handle_manual_zone_command(level)
            return

    def onHeartbeat(self):
        now = datetime.now()

        if self.debug:
            Domoticz.Debug(
                f"Heartbeat mode={self.mode} active={self.run_active} "
                f"type={self.run_type} zone={self.current_zone} end={self.zone_end_time}"
            )

        # Lancement automatique une seule fois par date, à partir de l'heure Mode3.
        if self.mode == MODE_AUTO and not self.run_active and self._time_reached(now):
            if self.last_auto_date != now.date():
                self.start_sequence("auto")
                self.last_auto_date = now.date()

        # Gestion non bloquante de la séquence en cours.
        if self.run_active and self.zone_end_time and now >= self.zone_end_time:
            if self.run_type in ("auto", "test"):
                self._advance_sequence(now)
            elif self.run_type == "manual":
                self.stop_sequence(reset_manual_selector=True)

    # ---------------------------------------------------------------------
    # Commands
    # ---------------------------------------------------------------------

    def _handle_mode_command(self, level):
        if level not in (MODE_OFF, MODE_AUTO, MODE_TEST):
            level = MODE_AUTO

        self.mode = level
        self._update_selector(UNIT_MODE, level)

        if level == MODE_OFF:
            self.stop_sequence(reset_manual_selector=True)
            Domoticz.Log("Irrigation mode: Off")

        elif level == MODE_AUTO:
            # Auto attend simplement la prochaine heure programmée.
            if self.run_type == "test":
                self.stop_sequence(reset_manual_selector=True)
            Domoticz.Log("Irrigation mode: Auto")

        elif level == MODE_TEST:
            # Test complet immédiat: 1 minute par zone.
            Domoticz.Log("Irrigation mode: Test - starting 1 minute per zone")
            self.start_sequence("test")

    def _handle_manual_zone_command(self, level):
        # Levels: 0=arrêt, 10=Zone1, 20=Zone2, ... 70=Zone7
        zone = int(level / 10) if level >= 10 else 0

        if zone < 0 or zone > 7:
            zone = 0

        if zone == 0:
            if self.run_type == "manual":
                self.stop_sequence(reset_manual_selector=False)
            self._update_selector(UNIT_MANUAL_ZONE, 0)
            return

        # Une commande manuelle annule une séquence en cours pour éviter 2 zones ouvertes.
        if self.run_active:
            self.stop_sequence(reset_manual_selector=False)

        self.start_manual_zone(zone)

    # ---------------------------------------------------------------------
    # Irrigation state machine
    # ---------------------------------------------------------------------

    def start_sequence(self, run_type):
        if len(self.zone_idxs) != 7:
            Domoticz.Error("Cannot start irrigation: Mode2 must contain exactly 7 zone idx")
            return
        if not self.main_valve_idxs:
            Domoticz.Error("Cannot start irrigation: Mode1 general valve idx missing")
            return

        self.stop_sequence(reset_manual_selector=True, quiet=True)
        self.run_active = True
        self.run_type = run_type
        self.current_zone = 0
        self._start_current_zone(datetime.now())

    def start_manual_zone(self, zone):
        if zone < 1 or zone > 7 or len(self.zone_idxs) != 7:
            Domoticz.Error(f"Invalid manual zone: {zone}")
            self._update_selector(UNIT_MANUAL_ZONE, 0)
            return

        self.run_active = True
        self.run_type = "manual"
        self.current_zone = zone
        self.zone_end_time = datetime.now() + timedelta(minutes=1)

        self._open_only_zone(zone - 1)
        self._update_selector(UNIT_MANUAL_ZONE, zone * 10)
        Domoticz.Log(f"Manual irrigation: Zone {zone} On for max 1 minute")

    def _start_current_zone(self, now):
        duration = 1 if self.run_type == "test" else self.zone_minutes[self.current_zone]
        duration = max(0, float(duration))

        if duration <= 0:
            Domoticz.Log(f"Skipping zone {self.current_zone + 1}: duration is 0")
            self._advance_sequence(now)
            return

        self.zone_end_time = now + timedelta(minutes=duration)
        self._open_only_zone(self.current_zone)
        Domoticz.Log(
            f"Irrigation {self.run_type}: Zone {self.current_zone + 1} On "
            f"for {duration:g} minute(s)"
        )

    def _advance_sequence(self, now):
        self.current_zone += 1
        if self.current_zone >= 7:
            self.stop_sequence(reset_manual_selector=True)
            Domoticz.Log(f"Irrigation {self.run_type or ''}: sequence finished")
            return
        self._start_current_zone(now)

    def stop_sequence(self, reset_manual_selector=True, quiet=False):
        self.run_active = False
        self.run_type = None
        self.current_zone = 0
        self.zone_end_time = None
        self._all_valves_off()
        if reset_manual_selector:
            self._update_selector(UNIT_MANUAL_ZONE, 0)
        if not quiet:
            Domoticz.Log("Irrigation stopped: all valves Off")

    # ---------------------------------------------------------------------
    # Valve helpers
    # ---------------------------------------------------------------------

    def _open_only_zone(self, zone_index):
        # Ordre volontaire: fermer les zones, ouvrir générale, puis ouvrir zone.
        for idx in self.zone_idxs:
            self._switch_idx(idx, "Off")

        for idx in self.main_valve_idxs:
            self._switch_idx(idx, "On")

        self._switch_idx(self.zone_idxs[zone_index], "On")

    def _all_valves_off(self):
        for idx in self.zone_idxs:
            self._switch_idx(idx, "Off")
        for idx in self.main_valve_idxs:
            self._switch_idx(idx, "Off")

    def _switch_idx(self, idx, command):
        if not idx:
            return False
        res = DomoticzAPI(f"type=command&param=switchlight&idx={idx}&switchcmd={command}")
        if not res or str(res.get("status", "")).lower() != "ok":
            Domoticz.Error(f"Valve command failure idx={idx} command={command}")
            return False
        if self.debug:
            Domoticz.Debug(f"Valve idx={idx} => {command}")
        return True

    # ---------------------------------------------------------------------
    # Setup helpers
    # ---------------------------------------------------------------------

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
        if UNIT_MODE not in Devices:
            options = {
                "LevelActions": "|||",
                "LevelNames": "Off|Auto|Test",
                "LevelOffHidden": "false",
                "SelectorStyle": "0",
            }
            Domoticz.Device(
                Unit=UNIT_MODE,
                Name="Irrigation Mode",
                TypeName="Selector Switch",
                Switchtype=18,
                Image=9,
                Options=options,
                Used=1,
            ).Create()

        if UNIT_MANUAL_ZONE not in Devices:
            options = {
                "LevelActions": "||||||||",
                "LevelNames": "0-Arret|Zone 1|Zone 2|Zone 3|Zone 4|Zone 5|Zone 6|Zone 7",
                "LevelOffHidden": "false",
                "SelectorStyle": "0",
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

        self._update_selector(UNIT_MODE, MODE_AUTO)
        self._update_selector(UNIT_MANUAL_ZONE, 0)

    def _restore_mode_from_device(self):
        try:
            saved = int(Devices[UNIT_MODE].sValue)
        except Exception:
            saved = MODE_AUTO
        if saved not in (MODE_OFF, MODE_AUTO, MODE_TEST):
            saved = MODE_AUTO
        self.mode = saved

    # ---------------------------------------------------------------------
    # Generic helpers
    # ---------------------------------------------------------------------

    def _time_reached(self, now):
        hour, minute = [int(x) for x in self.start_time.split(":")]
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return now >= scheduled

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
                Domoticz.Error(f"Domoticz API returned ERR for {APICall}")
                resultJson = None
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


# Glue - Plugin functions
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
