class State:
    """Base class for states in the state machine"""
    def __init__(self, name):
        self.name = name
        
    def enter(self, machine):
        """Called when entering the state"""
        pass
        
    def exit(self, machine):
        """Called when exiting the state"""
        pass
        
    def update(self, machine):
        """Called periodically to update the state"""
        pass


class StateMachine:
     """Simple state machine implementation"""
     def __init__(self, initial_state=None):
         self.states = {}
         self.current_state = None
         self.previous_state = None
         self.data = {}  # Shared data between states
         
         if initial_state:
             self.add_state(initial_state)
             self.set_state(initial_state.name)
     
     def add_state(self, state):
         """Add a state to the state machine"""
         self.states[state.name] = state
         
     def set_state(self, state_name):
         """Change to a new state"""
         if state_name not in self.states:
             raise ValueError(f"State {state_name} does not exist")
             
         # Exit current state
         if self.current_state:
             self.states[self.current_state].exit(self)
             
         # Update state tracking
         self.previous_state = self.current_state
         self.current_state = state_name
         
         # Enter new state
         self.states[self.current_state].enter(self)
         
     def update(self):
         """Update the current state"""
         if self.current_state:
             self.states[self.current_state].update(self)
     
     def mqtt_loop(self):
         """
         Safely call mqtt_client.loop() with error handling.
         Catches MMQTTStateError which occurs when calling loop() on a disconnected client.
         Logs the error but continues execution—the main loop's MQTTConnectionManager
         handles reconnection on its own schedule.
         """
         mqtt_client = self.data.get("mqtt_client")
         if not mqtt_client:
             return
         
         try:
             mqtt_client.loop()
         except Exception as e:
             # Catch MMQTTStateError and other exceptions from disconnected MQTT client
             # The string representation will contain the exception type name
             if "MMQTTStateError" in str(type(e)) or "not connected" in str(e).lower():
                 # MQTT client is disconnected - log quietly and continue
                 # The main loop's MQTTConnectionManager will handle reconnection
                 pass
             else:
                 # Other exceptions should be raised to be handled by the state machine
                 raise
             
     def get_state(self):
         """Get the current state name"""
         return self.current_state 