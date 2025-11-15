import time
import os
import wifi
import socketpool
import ssl

from adafruit_minimqtt.adafruit_minimqtt import MQTT, MMQTTException
import adafruit_logging as logging

from state_machine import StateMachine, State
from hw_test import TestMotion
from hardware import get_hardware
from airvent import create_vent_from_env
from vent_closer import VentCloser

logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, os.getenv("LOGGING_LEVEL", "INFO"), logging.INFO))

class IdleState(State):
    """
    Idle state.
    """
    def __init__(self):
        super().__init__("idle")

    def enter(self, machine):
        hardware = machine.data["hardware"]
        hardware.motor.release()
        logger.info("Idle")

    def update(self, machine):
        mqtt_client = machine.data["mqtt_client"]
        mqtt_client.loop()


class Calibrate(State):
    """
    Calibrate the motor/encoder by observing the encoder position when moving it back and forth a specified number of times.
    The motion should be first to fully open the air vent, then to fully close it.
    Assumes the encoder direction pin is pulled high.
    """

    def __init__(self):
        super().__init__("calibrate_high")

    def enter(self, machine):
        hardware = machine.data["hardware"]
        hardware.motor.release()

    def update(self, machine):
        hardware = machine.data["hardware"]


def init_mqtt_client(
    message_callback,
    mqtt_host=os.getenv('MQTT_HOST'),
    mqtt_user=os.getenv('MQTT_USER'),
    mqtt_password=os.getenv('MQTT_PASSWORD'),
    command_topic=None
):
    # Create a socket pool
    pool = socketpool.SocketPool(wifi.radio)
    ssl_context = ssl.create_default_context()
    mqtt_client = MQTT(
        broker=mqtt_host,
        port=1883,
        username=mqtt_user,
        password=mqtt_password,
        socket_pool=pool,
        ssl_context=ssl_context,
    )

    def connected(client, userdata, flags, rc):
        # This function will be called when the client is connected
        # successfully to the broker.
        logger.info("Connected to %s! Listening for commands on %s", mqtt_host, command_topic)
        # Subscribe to all changes on the onoff_feed.
        client.subscribe(command_topic)

    def disconnected(client, userdata, rc):
        # This method is called when the client is disconnected
        logger.warning("Disconnected from %s!", mqtt_host)

    # Setup the callback methods above
    mqtt_client.on_connect = connected
    mqtt_client.on_disconnect = disconnected
    mqtt_client.on_message = message_callback

    return mqtt_client


def init_state_machine(mqtt_client, hardware, vent):

    # Create the state machine
    machine = StateMachine()
    machine.data["hardware"] = hardware
    machine.data["mqtt_client"] = mqtt_client
    machine.data["vent"] = vent
    machine.add_state(TestMotion(moves_each_direction=2, target_step_angle=30.0))
    machine.add_state(IdleState())
    machine.add_state(VentCloser(30*60))
    machine.set_state("idle")
    return machine


if __name__ == "__main__":

    mqtt_topic=os.getenv('MQTT_TOPIC')
    command_topic = mqtt_topic + "/command"
    def message_callback(client, topic, message):
        global machine
        # This method is called when a topic the client is subscribed to
        # has a new message.
        if topic == command_topic:
            if message == "test":
                machine.set_state("test_motion")
            elif message == "close":
                machine.set_state("linear_time_close")
            elif message == "stop":
                machine.set_state("idle")
            else:
                logger.warning("Unknown command: %s", message)
        else:
            logger.info("New message on topic %s: %s", topic, message)

    mqtt_client = init_mqtt_client(message_callback, command_topic=command_topic)
    try:
        logger.info("Connecting to MQTT...")
        mqtt_client.connect()
        mqtt_client.publish(mqtt_topic + "/status", "Hello, world!")
    except MMQTTException as e:
        logger.error(f"Failed to connect to MQTT: {e}")

    # Get the hardware interface
    hardware = get_hardware()
    vent = create_vent_from_env()
    machine = init_state_machine(mqtt_client, hardware, vent)

    # Main loop
    while True:
        machine.update()


    # old code from main loop
    loop_counter = 0
    total_loop_time = 0
    while False:
        machine.update()
        if mqtt_client.is_connected():
            loop_start = time.monotonic_ns()
            mqtt_client.loop()
            loop_end = time.monotonic_ns()
            loop_time = loop_end - loop_start
            total_loop_time += loop_time
            loop_counter += 1
            if loop_counter % 100 == 0:
                logger.debug(f"Average loop time: {total_loop_time / loop_counter / 1000} us")
        else:
            time.sleep(0.01)
