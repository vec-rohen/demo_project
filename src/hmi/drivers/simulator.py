"""A small process simulation that behaves like a PLC running a heated tank.

Physical model (one scan every ``dt`` seconds):

* A variable-speed pump fills a 200 L tank; flow and motor load rise with speed.
* An outlet valve and a drain valve empty it, with flow proportional to sqrt(level).
* A heater warms the tank; cold inflow and ambient losses cool it.

PLC logic mirrors what a real program would do: interlocks (E-stop, high level
switch, heater low-level cut-out) always apply; in automatic mode a PI loop
drives pump speed to the level target and the heater runs on/off with
hysteresis around the temperature target.
"""

from __future__ import annotations

import asyncio
import math
import random
import time

from ..tags import TagDatabase
from .base import Driver, Value

TANK_LITRES = 200.0
PUMP_MAX_LPM = 100.0
OUTLET_MAX_LPM = 60.0
DRAIN_MAX_LPM = 40.0
INLET_TEMP_C = 15.0
AMBIENT_C = 20.0
HEATER_RATE_C_PER_S = 0.35


class SimulatorDriver(Driver):
    supports_simulated_inputs = True

    def __init__(self, db: TagDatabase, dt: float = 0.1, seed: int | None = None):
        super().__init__(db)
        self.dt = dt
        self.rng = random.Random(seed)
        self._task: asyncio.Task | None = None
        self._integral = 30.0
        self.values: dict[str, Value] = {
            "DI_EStopOK": True,
            "DI_PumpRunning": False,
            "DI_LevelHighSw": False,
            "DI_LevelLowSw": False,
            "DI_DoorClosed": True,
            "DO_PumpRun": False,
            "DO_HeaterOn": False,
            "DO_DrainValve": False,
            "DO_AlarmBeacon": False,
            "AI_TankLevel": 35.0,
            "AI_TankTemp": 21.0,
            "AI_InletFlow": 0.0,
            "AI_OutletFlow": 0.0,
            "AI_PumpPressure": 0.0,
            "AI_MotorCurrent": 0.0,
            "AO_PumpSpeed": 0.0,
            "AO_OutletValve": 40.0,
            "SP_AutoMode": True,
            "SP_LevelTarget": 60.0,
            "SP_TempTarget": 45.0,
            "SP_LevelHiAlarm": 85.0,
            "SP_TempHiAlarm": 60.0,
            "SP_LevelKp": 2.5,
        }
        # The heated-tank model needs all of its tags; any other asset gets
        # placeholder values that operators can still write and trend.
        self._tank_model = all(name in db.tags for name in self.values)
        self.values = {k: v for k, v in self.values.items() if k in db.tags}
        for tag in db.tags.values():
            if tag.name not in self.values:
                if tag.type == "bool":
                    self.values[tag.name] = False
                elif tag.min is not None and tag.max is not None:
                    self.values[tag.name] = round((tag.min + tag.max) / 2, tag.decimals)
                else:
                    self.values[tag.name] = 0.0
        # The pump motor takes a moment to respond, like a real starter.
        self._pump_feedback_delay = 0.0

    async def start(self) -> None:
        self.connected = True
        self._task = asyncio.create_task(self._run(), name="plc-simulator")

    async def stop(self) -> None:
        self.connected = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def read_all(self) -> dict[str, Value]:
        return dict(self.values)

    async def write(self, name: str, value: Value) -> None:
        self.values[name] = value
        if name == "SP_AutoMode" and value:
            # Bumpless transfer: seed the integrator with the current speed.
            self._integral = float(self.values.get("AO_PumpSpeed", 0.0))

    async def simulate_input(self, name: str, value: Value) -> None:
        """Force a physical input (E-stop, door switch…) for testing the HMI."""
        self.values[name] = value

    async def _run(self) -> None:
        next_tick = time.monotonic()
        while True:
            self.step(self.dt)
            next_tick += self.dt
            await asyncio.sleep(max(0.0, next_tick - time.monotonic()))

    # -- one PLC scan + physics update ------------------------------------
    def step(self, dt: float) -> None:
        if not self._tank_model:
            self._step_generic()
            return
        v = self.values
        level = float(v["AI_TankLevel"])
        temp = float(v["AI_TankTemp"])

        # Discrete inputs derived from the process.
        v["DI_LevelHighSw"] = level >= 97.0
        v["DI_LevelLowSw"] = level <= 5.0

        # Automatic control.
        if v["SP_AutoMode"]:
            error = float(v["SP_LevelTarget"]) - level
            kp = float(v["SP_LevelKp"])
            ki = 0.05 * kp
            unclamped = kp * error + self._integral + ki * error * dt
            if 0.0 < unclamped < 100.0:  # anti-windup: only integrate inside limits
                self._integral += ki * error * dt
            speed = min(100.0, max(0.0, kp * error + self._integral))
            v["AO_PumpSpeed"] = round(speed, 2)
            v["DO_PumpRun"] = speed > 2.0
            target = float(v["SP_TempTarget"])
            if temp < target - 0.5:
                v["DO_HeaterOn"] = True
            elif temp > target + 0.5:
                v["DO_HeaterOn"] = False

        # Interlocks always win over commands.
        pump_permitted = bool(v["DI_EStopOK"]) and not v["DI_LevelHighSw"]
        heater_permitted = bool(v["DI_EStopOK"]) and level > 15.0
        pump_on = bool(v["DO_PumpRun"]) and pump_permitted
        heater_on = bool(v["DO_HeaterOn"]) and heater_permitted
        if not v["DI_EStopOK"]:
            v["DO_PumpRun"] = False
            v["DO_HeaterOn"] = False

        # Pump starter feedback lags the command by ~0.5 s.
        if pump_on:
            self._pump_feedback_delay = min(0.5, self._pump_feedback_delay + dt)
        else:
            self._pump_feedback_delay = 0.0
        v["DI_PumpRunning"] = pump_on and self._pump_feedback_delay >= 0.5

        speed_frac = float(v["AO_PumpSpeed"]) / 100.0 if v["DI_PumpRunning"] else 0.0
        inflow = PUMP_MAX_LPM * speed_frac
        head = math.sqrt(max(level, 0.0) / 100.0)
        outflow = OUTLET_MAX_LPM * float(v["AO_OutletValve"]) / 100.0 * head
        if v["DO_DrainValve"]:
            outflow += DRAIN_MAX_LPM * head

        volume = level / 100.0 * TANK_LITRES
        volume += (inflow - outflow) / 60.0 * dt
        volume = min(TANK_LITRES, max(0.0, volume))
        level = volume / TANK_LITRES * 100.0

        heat = HEATER_RATE_C_PER_S if heater_on else 0.0
        losses = 0.004 * (temp - AMBIENT_C)
        mixing = (inflow / 60.0) * (temp - INLET_TEMP_C) / max(volume, 20.0)
        temp += (heat - losses - mixing) * dt

        noise = self.rng.gauss
        v["AI_TankLevel"] = round(level, 3)
        v["AI_TankTemp"] = round(temp, 3)
        v["AI_InletFlow"] = round(max(0.0, inflow + (noise(0, 0.3) if inflow else 0.0)), 2)
        v["AI_OutletFlow"] = round(max(0.0, outflow + (noise(0, 0.3) if outflow else 0.0)), 2)
        running = bool(v["DI_PumpRunning"])
        v["AI_PumpPressure"] = round(
            max(0.0, 0.8 + 6.0 * speed_frac**2 + noise(0, 0.03)) if running else 0.0, 3
        )
        v["AI_MotorCurrent"] = round(
            max(0.0, 3.2 + 10.0 * speed_frac**2 + noise(0, 0.08)) if running else 0.0, 2
        )
        v["DO_AlarmBeacon"] = (
            not v["DI_EStopOK"] or bool(v["DI_LevelHighSw"]) or not v["DI_DoorClosed"]
        )

    def _step_generic(self) -> None:
        """Gentle random walk on analog inputs so trends and alarms have something to show."""
        for tag in self.db.tags.values():
            if tag.kind == "AI" and tag.min is not None and tag.max is not None:
                span = tag.max - tag.min
                value = float(self.values[tag.name]) + self.rng.gauss(0, span * 0.002)
                self.values[tag.name] = round(min(tag.max, max(tag.min, value)), 4)
