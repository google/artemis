import time
import uiautomator2 as u2


def test():
    device = u2.connect("emulator-5554")
    print("Testing send_keys with immediate IME switch off")
    device.set_fastinput_ime(True)
    device.send_keys("Test 1")
    device.set_fastinput_ime(False)

    time.sleep(2)
    print("Testing send_keys with delay before IME switch off")
    device.set_fastinput_ime(True)
    device.send_keys("Test 2")
    time.sleep(1.0)
    device.set_fastinput_ime(False)


if __name__ == "__main__":
    test()
