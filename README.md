# Hearthmate

An experimental robot that controls a wood stove air vent to achieve optimal combustion. Optimal combustion maximizes heat retention and minimizing creosote buildup.

## Hardware

- [Adafruit ESP32 Feather v2](https://learn.adafruit.com/adafruit-esp32-feather-v2/)
- [Adafruit Stepper + DC Motor FeatherWing](https://learn.adafruit.com/adafruit-stepper-dc-motor-featherwing/)
- 2.3 Kg*cm NEMA16 Bipolar stepper
- [ams AS5600 position sensor](https://ams-osram.com/products/sensor-solutions/position-sensors/ams-as5600-position-sensor)
- MLX 90640 thermal 32x24 IR array
- TMP36 analog temperature sensor
- 5:1 gear ratio belt and pulley connecting motor to drive shaft
- mechanical linkage from drive shaft to vent control lever arm

## Software stack

- [CircuitPython 10.0.3](https://docs.circuitpython.org/en/stable/docs/index.html)
- [Adafruit Motorkit library](https://docs.circuitpython.org/projects/motorkit/en/latest/)
- [Adafruit motor library](https://docs.circuitpython.org/projects/motor/en/latest/)
- [Adafruit MiniMQTT library](https://docs.circuitpython.org/projects/minimqtt/en/latest/)
- Plus each library's respective dependencies

## TODO

- [Home Assistant MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery) - DONE
- resilience against wifi/mqtt disconnection
- stovelink seq num, burn time, and other state info recovery after reboot: use mqtt retain
- handle camera disconnection gracefully
- Back-off logic based on camera data
- Ennumeration for direction in Vent class
- Add HA diagnostic entity to track motor step slippage % over movement from open->closed: 
        (steps_needed - actual_steps) / steps_needed
- type annotations