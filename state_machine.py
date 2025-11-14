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
            
    def get_state(self):
        """Get the current state name"""
        return self.current_state 