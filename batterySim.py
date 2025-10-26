#!/usr/bin/env python3
import sys, math
from textwrap import dedent
import caproto
from caproto import ChannelType
from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run
import logging

# Configure logging to display INFO level messages
logging.basicConfig(filename='batterySim.log', level=logging.INFO)
logger = logging.getLogger(__name__)

# Global constants
seconds_per_hour = 3600
hours_per_second = 1 / seconds_per_hour

class BatteryChargeIOC(PVGroup):
    """
    An IOC with read/writable PVs.

    Scalar PVs
    ----------
    V_real (real battery voltage)
    I_real (real battery current)
    V_sim (simulated battery voltage)
    I_sim (simulated battery current)
    V_target (target battery voltage)
    Solar_power (the power from the solar panel)
    Eclipse (Whether or not the satellite experiences the eclipse: 0 Non-Eclipse, 1 Eclipse)

    """

    # Battery features
    battery_capacity = 150     # Wh
    battery_nominal_voltage = 34    # Volts
    model_constant = 80    # Wh  --- a constant in a simple model: Energy = model_constant (exp^(V/Vn) - 1), Vn: battery_nominal_voltage
    max_voltage = math.log(battery_capacity / model_constant + 1) * battery_nominal_voltage    # Volts
    solar_power_max = 150    # W
    load_power_const = 100    # W  --- assuming a constant energy consumption from the satellite
    
    V_real  = pvproperty(
        name='Vreal',
        value=34.02,
        doc='real battery voltage',
        units='Volts',
        precision=2,
        record='ai'
    )

    I_real  = pvproperty(
        name='Ireal',
        value=-0.24,
        doc='real battery current',
        units='Amps',
        precision=2,
        record='ai'
    )

    V_sim  = pvproperty(
        name='Vsim',
        value=32.0,
        doc='simulated battery voltage',
        units='Volts',
        precision=2,
        record='ai'
    )

    I_sim = pvproperty(
        name='Isim',
        value=0.0,
        doc='simulated battery current',
        units='Amps',
        precision=2,
        record='ai'
    )

    V_target = pvproperty(
        name='Vtarget',
        value=34.0,
        doc='target battery voltage',
        units='Volts',
        precision=2,
        record='ai'
    )

    Solar_power = pvproperty(
        name='SolarPower',
        value=110.0,
        doc='Power from solar panel',
        units='Watts',
        precision=2,
        record='ai'
    )

    Eclipse = pvproperty(
        name='Eclipse',
        doc='Wheather the satellite experiences Eclipse',
        dtype=ChannelType.ENUM,
        enum_strings=['Non-Eclipse', 'Eclipse'],
        value=0,
        record='mbbi'
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initOK = True
        # set initial values
        self.sim_time = 0.0   # seconds
        self.eclipse_half_duration = 3600   # seconds
        self.init_solar_power = self.Solar_power.value
        logger.info(f"=== Creating the Simulator ===")
        logger.info(f"This battery allows maximum voltage: Vmax = {self.max_voltage}")

        # check if default values are set correctly
        if (self.V_sim.value < 0 or self.V_sim.value > self.max_voltage):
            logger.critical(f"Default battery voltage {self.V_sim.value} is out of the allowed range [0, {self.max_voltage}]")
            self.initOK = False
            return
        if (self.Solar_power.value < 0 or self.Solar_power.value > self.solar_power_max):
            logger.critical(f"Default solar power {self.Solar_power.value} is out of the allowed range [0, {self.solar_power_max}]")
            self.initOK = False
            return
        if (self.V_target.value < 0 or self.V_target.value > self.max_voltage):
            logger.critical(f"Default target voltage {self.V_target.value} is out of the allowed range [0, {self.max_voltage}]")
            self.initOK = False
            return
        if (self.Eclipse.value == 1):
            self.eclipse_begin = self.sim_time

    @V_sim.startup
    async def V_sim(self, instance, async_lib):
        """Startup method for V_sim PV. Runs the main simulation loop."""
        logger.info("== Simulation loop started ==")

        # simulation parameters
        sim_interval = 1    # senconds
        deltaT = sim_interval * hours_per_second    # hours
        while True:
            # get current PV values
            Vsim_value = self.V_sim.value
            target_V = self.V_target.value
            sp_value = self.Solar_power.value
            charging_power = sp_value - self.load_power_const

            # update V_sim and I_sim
            if (charging_power > 0 and Vsim_value < target_V):
                # charging the battery
                tmp = math.exp(Vsim_value / self.battery_nominal_voltage)
                deltaV = charging_power * deltaT * self.battery_nominal_voltage / (self.model_constant * tmp)
                Vsim_value += deltaV
                Vsim_value = min(Vsim_value, target_V)
                await self.V_sim.write(Vsim_value)
                await self.I_sim.write(0.0)
            elif (charging_power < 0):
                # discharging the battery
                Isim_value = charging_power / Vsim_value
                tmp = math.exp(Vsim_value / self.battery_nominal_voltage)
                deltaV = charging_power * deltaT * self.battery_nominal_voltage / (self.model_constant * tmp)
                Vsim_value += deltaV
                Vsim_value = max(Vsim_value, 0)
                await self.V_sim.write(Vsim_value)
                await self.I_sim.write(Isim_value)

            #update Solar_power
            if(self.Eclipse.value == 1):
                tmp = (self.sim_time - self.eclipse_begin) / self.eclipse_half_duration
                if (tmp > 0 and tmp <=1):
                    solar_power = self.init_solar_power * math.cos(0.5 * math.pi * tmp)
                    await self.Solar_power.write(solar_power)
                elif (tmp > 1 and tmp <= 2):
                    solar_power = - self.init_solar_power * math.cos(0.5 * math.pi * tmp)
                    await self.Solar_power.write(solar_power)
                elif (tmp > 2):
                    await self.Eclipse.write(0)

            # next step
            self.sim_time += sim_interval
            await async_lib.library.sleep(0.1 * sim_interval)

    @V_target.putter
    async def V_target(self, instance, value):
        """Set target voltage for charging, value range [0 Vmax] Volts"""
        logger.info(f"Setting V_target(target voltage): requested={value}, current={instance.value}")
        new_value = max(0.0, min(value, self.max_voltage))
        logger.info(f"V_target set to {new_value}")
        return new_value

    @Eclipse.putter
    async def Eclipse(self, instance, value):
        """Handle writes to Eclipse PV, changing eclipse status."""
        if isinstance(value, str):
            enum_strings = self.Eclipse.enum_strings
            try:
                new_value = enum_strings.index(value)
                if(new_value == 1 and instance.value == 0):
                    logger.info("== Going into eclipse. ==")
                    self.eclipse_begin = self.sim_time
                elif(new_value == 0 and instance.value == 1):
                    logger.info("== Reset to non-eclipse. ==")
                    await self.Solar_power.write(self.init_solar_power)
                return new_value
            except ValueError:
                logger.info(f"Invalid State string '{value}', reverting to {instance.value}")
                return instance.value
        elif isinstance(value, (int, float)) and int(value) in range(2):
            if(int(value) == 1 and instance.value == 0):
                logger.info("== Going into eclipse. ==")
                self.eclipse_begin = self.sim_time
            elif(int(value) == 0 and instance.value == 1):
                logger.info("== Reset to non-eclipse. ==")
                await self.Solar_power.write(self.init_solar_power)
            return int(value)
        else:
            logger.info(f"Invalid State '{value}', reverting to {instance.value}")
        return instance.value


if __name__ == '__main__':
    ioc_options, run_options = ioc_arg_parser(
        default_prefix='BC:',
        desc=dedent(BatteryChargeIOC.__doc__))
    ioc = BatteryChargeIOC(**ioc_options)
    if (not ioc.initOK):
        print("Initialization failed!")
        logger.critical("Initialization failed!")
        sys.exit(1)
    run(ioc.pvdb, **run_options)
