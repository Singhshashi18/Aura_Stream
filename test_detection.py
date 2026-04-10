#!/usr/bin/env python
"""Quick test of speech detection functions"""
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'aura_stream.settings')
import django
django.setup()

from core.consumers import _is_likely_speech, _estimate_rms_variance

print("=== Speech/Noise Detection Tests ===\n")

# Test 1: Silence (RMS below threshold)
is_speech, detection = _is_likely_speech(100, [], 420.0)
assert not is_speech and detection == "silence", f"Test 1 failed: {is_speech=}, {detection=}"
print("✓ Test 1 - Silence (low RMS): PASS")

# Test 2: Steady background noise (low variance, steady tone)
rms_window = [500, 505, 502, 498, 501, 500, 502, 499, 501, 500]
variance = _estimate_rms_variance(rms_window)
is_speech, detection = _is_likely_speech(500, rms_window, 420.0)
print(f"  - Steady noise variance: {variance:.2f}")
assert not is_speech and detection == "background_noise", f"Test 2 failed: {is_speech=}, {detection=}"
print("✓ Test 2 - Steady noise (doorbell pattern): PASS")

# Test 3: User speech (high variance, modulating energy)
rms_window = [450, 520, 480, 600, 420, 580, 470, 610, 490, 550]
variance = _estimate_rms_variance(rms_window)
is_speech, detection = _is_likely_speech(550, rms_window, 420.0)
print(f"  - Speech variance: {variance:.2f}")
assert is_speech and detection == "user_speech", f"Test 3 failed: {is_speech=}, {detection=}"
print("✓ Test 3 - User speech (high variance): PASS")

# Test 4: Variance calculation
var1 = _estimate_rms_variance([100])
assert var1 == 0, "Single value should have 0 variance"
print("✓ Test 4 - Variance single value: PASS")

var2 = _estimate_rms_variance([100, 100, 100])
assert var2 == 0, "Constant values should have 0 variance"
print("✓ Test 5 - Variance constant: PASS")

print("\n✓✓✓ All detection tests PASSED! ✓✓✓")
