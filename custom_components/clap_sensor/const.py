"""Constants for the Clap Sensor HACS Integration."""

DOMAIN = "clap_sensor"
SIGNAL_CLAP_EVENT = f"{DOMAIN}_clap_event"
EVENT_CLAP_DETECTED = "clap_detected"

CONF_INPUT_DEVICE = "input_device"
CONF_MIN_AMPLITUDE = "min_amplitude"
CONF_SNR_THRESHOLD_DB = "snr_threshold_db"
CONF_MIN_HIGH_FREQ_RATIO = "min_high_freq_ratio"
CONF_ENABLE_TRIPLE_CLAP = "enable_triple_clap"

DEFAULT_INPUT_DEVICE = "default"
DEFAULT_MIN_AMPLITUDE = 0.05
DEFAULT_SNR_THRESHOLD_DB = 12.0
DEFAULT_MIN_HIGH_FREQ_RATIO = 0.30
DEFAULT_ENABLE_TRIPLE_CLAP = True
