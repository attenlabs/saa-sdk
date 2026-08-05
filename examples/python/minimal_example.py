#!/usr/bin/env python3
"""Minimal SAA example - shows how to use the SDK with generated audio.

This is a minimal integration example that doesn't require real hardware.
It generates a simple test tone and feeds it to SAA using feed_audio().
Useful for testing the SDK's basic functionality.
"""

import os
import time
import numpy as np
from saa import AttentionClient


# Generate a fake 1-second test tone (440Hz) at 16kHz mono PCM16
def generate_test_tone(duration=1.0, sample_rate=16000, frequency=440):
    """Generate a simple sine wave tone for testing."""
    t = np.linspace(0, duration, int(sample_rate * duration))
    tone = (np.sin(2 * np.pi * frequency * t) * 32767).astype(np.int16)
    return tone.tobytes()


def main():
    # Get API key from environment variable
    api_key = os.environ.get("SAA_API_KEY")
    
    if not api_key:
        print("❌ Error: SAA_API_KEY environment variable not set")
        print("   Set it with: export SAA_API_KEY=your_key_here")
        return 1
    
    # Initialize the client in feed mode (we'll feed audio manually)
    client = AttentionClient(
        token=api_key,
        enable_audio=False,  # We'll feed audio manually
        enable_video=False,  # No video for this example
    )
    
    # Event handlers
    @client.on_turn_ready
    def on_turn_ready(event):
        print(f"✅ TURN DETECTED: duration={event.duration_sec:.2f}s")
    
    @client.on_warmup_complete
    def on_warmup():
        print("✅ SAA is ready and warmed up!")
    
    @client.on_error
    def on_error(event):
        print(f"❌ Error: {event.title} - {event.message}")
    
    @client.on_disconnected
    def on_disconnected(event):
        print(f"Disconnected: {event.reason or 'unknown reason'}")
    
    print("🚀 Starting SAA client...")
    client.start()
    
    print("⏳ Waiting for warmup... (this takes ~10-15 seconds)")
    time.sleep(15)  # Give time for model to warm up
    
    # Generate and feed a test tone
    print("🎵 Feeding test tone (1 second, 440Hz)...")
    test_audio = generate_test_tone()
    client.feed_audio(test_audio)
    
    # Wait for processing
    print("⏳ Waiting for SAA to process...")
    time.sleep(3)
    
    # Stop the client
    print("🛑 Stopping client...")
    client.stop()
    print("✅ Done!")
    
    return 0


if __name__ == "__main__":
    exit(main())
