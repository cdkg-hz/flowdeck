class ConfigurationNeeded(RuntimeError):
    pass


class WaitForDependency(RuntimeError):
    pass


class OutcomeUnknown(RuntimeError):
    pass


class Superseded(RuntimeError):
    pass


class LostLease(RuntimeError):
    pass
