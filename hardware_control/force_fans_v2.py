import os
import glob
import sys
import time

def write_file(path, value, description):
    try:
        with open(path, "w") as f:
            f.write(str(value))
        print(f"    [+] SUCCESS: Set {description} to {value}")
        return True
    except OSError as e:
        print(f"    [!] FAILED to set {description}: {e}")
        return False

def force_system():
    print("==================================================")
    print("   DIAMOND STATE: ADAPTIVE FAN OVERRIDE")
    print("==================================================")
    
    # Find all AMD GPU hardware monitors
    paths = glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*")
    
    if not paths:
        print("[!] CRITICAL: No GPU sensors found.")
        return

    for hwmon in paths:
        print(f"\n[*] Targeting Controller: {hwmon}")
        
        # 1. WAKE THE CARD (Force Performance Mode)
        # This disables "Zero RPM" idle features
        device_path = os.path.dirname(os.path.dirname(hwmon))
        power_path = os.path.join(device_path, "power_dpm_force_performance_level")
        if os.path.exists(power_path):
            print("    -> Setting Power Profile to 'profile_peak' (Wakes Fans, preserves MCLK OC)...")
            write_file(power_path, "profile_peak", "Power Profile")
        
        # 2. DETERMINE MAX PWM
        pwm_max_path = os.path.join(hwmon, "pwm1_max")
        max_speed = 255 # Default fallback
        if os.path.exists(pwm_max_path):
            try:
                with open(pwm_max_path, "r") as f:
                    max_speed = int(f.read().strip())
                print(f"    -> Detected Max PWM Ceiling: {max_speed}")
            except (OSError, ValueError):
                print("    -> Could not read max PWM, assuming 255.")
        
        # Calculate 95% of whatever the card said is max
        target_speed = int(max_speed * 0.95)
        
        # 3. FORCE MANUAL CONTROL
        enable_path = os.path.join(hwmon, "pwm1_enable")
        write_file(enable_path, "1", "Manual Mode (pwm1_enable)")
        
        # 4. INJECT SPEED (Adaptive)
        pwm_path = os.path.join(hwmon, "pwm1")
        print(f"    -> Attempting to write {target_speed} to controller...")
        if not write_file(pwm_path, target_speed, "PWM Speed"):
            # FAILSAFE: If raw integer failed, try percentage (0-100)
            print("    [!] Raw value rejected. Attempting Percentage Protocol...")
            write_file(pwm_path, "95", "Percentage Speed")

        # 5. VERIFY RPM (Give it 2 seconds to spin up)
        time.sleep(2)
        rpm_path = os.path.join(hwmon, "fan1_input")
        if os.path.exists(rpm_path):
            try:
                with open(rpm_path, "r") as f:
                    rpm = f.read().strip()
                print(f"    -> LIVE RPM: {rpm}")
            except (OSError, ValueError):
                print("    -> RPM Sensor Unreadable")

if __name__ == "__main__":
    if os.geteuid() != 0:
        print("CRITICAL: RUN WITH SUDO")
    else:
        force_system()
