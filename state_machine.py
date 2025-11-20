class State:
    """Base class for states in the state machine"""
    def __init__(self, name):
        self.name = name
        
    def enter(self, machine):
        """Called when entering the state"""
        pass

    def resume(self, machine):
        """Called when resuming the state from the stack"""
        self.enter(machine)
        
    def exit(self, machine):
        """Called when exiting the state"""
        pass
        
    def update(self, machine):
        """Called periodically to update the state"""
        pass

    def handle_move_request(self, machine, target_position):
        """
        Handle a request to move the vent to a specific position.
        Returns True if the request was accepted/handled, False otherwise.
        """
        return False


class StateMachine:
     """Simple state machine implementation"""
     def __init__(self, initial_state=None):
         self.states = {}
         self.current_state = None
         self.previous_state = None
         self.state_stack = []
         self.data = {}  # Shared data between states
         
         if initial_state:
             self.add_state(initial_state)
             self.set_state(initial_state.name)
     
     def add_state(self, state):
         """Add a state to the state machine"""
         self.states[state.name] = state
         
     def set_state(self, state_name):
         """Change to a new state"""
         self._transition(state_name)
         self.states[self.current_state].enter(self)

     def _transition(self, state_name):
         """Perform state transition steps (exit current, update tracking)"""
         if state_name not in self.states:
             raise ValueError(f"State {state_name} does not exist")
             
         # Exit current state
         if self.current_state:
             self.states[self.current_state].exit(self)
             
         # Update state tracking
         self.previous_state = self.current_state
         self.current_state = state_name
         
     def push_state(self, state_name):
         """Push the current state to the stack and switch to a new state"""
         if self.current_state:
             self.state_stack.append(self.current_state)
         self.set_state(state_name)
         
     def pop_state(self):
         """Pop the last state from the stack and switch to it"""
         if self.state_stack:
             prev_state = self.state_stack.pop()
             self._transition(prev_state)
             self.states[self.current_state].resume(self)
         else:
             # Fallback if stack is empty - perhaps stay or go to idle?
             # For now, do nothing or maybe log warning
             pass

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

     def handle_move_request(self, vent_position):
         """
         Attempt to handle a vent move request by delegating to the current state.
         Returns True if accepted, False otherwise.
         """
         if self.current_state and self.current_state in self.states:
             return self.states[self.current_state].handle_move_request(self, vent_position)
         return False
