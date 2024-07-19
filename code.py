import time, os, sys, json, random
import board, busio, keypad, rotaryio, digitalio
import rainbowio
import adafruit_midi # type: ignore
from adafruit_midi.note_on  import NoteOn
from adafruit_midi.note_off  import NoteOff
from adafruit_midi.pitch_bend  import PitchBend
from adafruit_midi.control_change  import ControlChange
import neopixel
from adafruit_ticks import ticks_ms, ticks_diff, ticks_add
from adafruit_macropad import MacroPad
import displayio, terminalio
from adafruit_display_text.bitmap_label import Label
import usb_midi
import ulab.numpy as np
import synthio
import audiopwmio
import audiomixer


keynum_to_padnum = (0, 4, -1, # pad nums go from bottom row of four: 0,1,2,3
                    1, 5, -1, # then above that, next row of four 4,5,6,7
                    2, 6, -1, # and top row are invalid pad nums (buttons used for transport)
                    3, 7, -1)

key_1 = 2
key_2= 5
key_3 = 8
key_4 = 11

piano_keys = [1,4,7,10,0,3,6,9]

key2note = {
    1: 0,
    4: 2,
    7: 4,
    10: 5,
    0: 7,
    3: 9,
    6: 11,
    9: 12,
    }

num2note = {
    0: "C",
    1: "C#",
    2: "D",
    3: "D#",
    4: "E",
    5: "F",
    6: "F#",
    7: "G",
    8: "G#",
    9: "A",
    10: "A#",
    11: "B",
    }

#chords = ["Maj", "Min", "Dim", "Maj7", "Min7"]

#https://darkworld.com/mythos/school/Chords-in-Major-Key-of-A.html
chords = ["tri", "7", "9", "add2", "11", "add4", "13", "add6", "sus2", ]
chord = 0

def get_note(button, scale, octave, type):
    if type == "midi":
        return key2note[button]+scale+(12*octave)
    else:
        if key2note[button] + scale > 11:
            return num2note[abs(key2note[button]+scale-12)]+str(octave+1)
        else:
            return num2note[key2note[button]+scale]+str(octave)

#macropad = MacroPad()
encoder = None
#
#===========#
debug = True
lights = True
#===========#

octave = 5 #this is 4 for some reason
scale = 0 #CMaj
bpm = 120
steps_per_beat = 1  # divisions per beat: 8 = 32nd notes, 4 = 16th notes

# display setup begin
dw,dh = 64,128
display = board.DISPLAY
display.rotation = 90
font = terminalio.FONT
mainscreen = displayio.Group()
display.root_group = mainscreen

leds = neopixel.NeoPixel(board.NEOPIXEL, 12, brightness=0.2, auto_write=False)
#leds.fill(0xff00ff); leds.show()

key_pins = (board.KEY1, board.KEY2, board.KEY3,
            board.KEY4, board.KEY5, board.KEY6,
            board.KEY7, board.KEY8, board.KEY9,
            board.KEY10, board.KEY11, board.KEY12)

keys = keypad.Keys(key_pins, value_when_pressed=False, pull=True)
encoder = rotaryio.IncrementalEncoder(board.ENCODER_B, board.ENCODER_A)  # yes, reversed
encoder_switch = keypad.Keys((board.ENCODER_SWITCH,), value_when_pressed=False, pull=True)


def update_step_millis():
    global step_millis
    # Beat timing assumes 4/4 time signature, e.g. 4 beats per measure, 1/4 note gets the beat
    beat_time = 60 / bpm  # time length of a single beat
    beat_millis = beat_time * 1000  # time length of single beat in milliseconds
    step_millis = int(beat_millis / 4) #steps_per_beat)  # time length of a beat subdivision, e.g. 1/16th note
    # and keep "step_millis" an int so diff math is fast

# sequencer state
step_millis = 0 # derived from bpm, changed by "update_step_millis()" below
last_step_millis = ticks_ms()
seq_pos = 0  # where in our sequence we are
light_pos = 0
piano_bg = ((255, 0, 255), (0, 255, 255), (255, 255, 0), (80, 80, 80))
playing = False
recording = False

# UI states
pads_lit = [0] * 8 
num_steps = 8  # number of steps, based on length of first stepline
last_led_millis = ticks_ms()  # last time we updated the LEDS
enc_sw_press_millis = 0
encoder_val_last = encoder.position
encoder_mode = 0  # 0 = change pattern, 1 = change kit, 2 = change bpm
led_min = 5  # how much to fade LEDs by
led_fade = 10 # how much to fade LEDs by
update_step_millis()

#SAMPLE_RATE = 44100  # clicks @ 36kHz & 48kHz on rp2040
SAMPLE_RATE = 44100
SAMPLE_SIZE = 256    # we like powers of two
VOLUME = 16384
#VOLUME = 25000

wave_saw = np.linspace(VOLUME, -VOLUME, num=SAMPLE_SIZE, dtype=np.int16)
wave_squ = np.concatenate((np.ones(SAMPLE_SIZE//2, dtype=np.int16)*VOLUME,np.ones(SAMPLE_SIZE//2, dtype=np.int16)*-VOLUME))
wave_sin = np.array(np.sin(np.linspace(0, 4*np.pi, SAMPLE_SIZE, endpoint=False)) * VOLUME, dtype=np.int16)
wave_noise = np.array([random.randint(-VOLUME, VOLUME) for i in range(SAMPLE_SIZE)], dtype=np.int16)
wave_sin_dirty = np.array( wave_sin + (wave_noise/4), dtype=np.int16)
waveforms = (wave_saw, wave_squ, wave_sin, wave_sin_dirty, wave_noise)
waveform_name = ["Saw", "Square", "Sine", "DistSine", "Noise"]
osc1_wave = 0
osc2_wave = 0

synth = synthio.Synthesizer(sample_rate=SAMPLE_RATE, channel_count=2)  # note: no envelope or waveform, we do that in Note now!
audio = audiopwmio.PWMAudioOut(board.SDA) # macropadsynthplug!

mixer = audiomixer.Mixer(channel_count=2, voice_count=2, 
                         sample_rate=SAMPLE_RATE, buffer_size=8192) #buffer_size=4096)
audio.play(mixer)
mixer.voice[0].play(synth)
mixer.voice[0].level = 1

#audio.play(synth) # attach mixer to audio playback


num_oscs_a = 1  # how many oscillators per note
num_oscs_b = 1
max_oscs = 5
osc_detune_a = 0.01 # how much detune (fatness)
osc_detune_b = 0.01 

notes_pressed = {}  # which notes are currently being pressed, and their note objects (so we can unpress them)
mod_val = 0.5  # ranges 0-1

a1, d1, s1, r1, lvl1 = 0.0, 0.0, 0.0, 0.0, 1.0
a2, d2, s2, r2, lvl2 = 0.0, 0.0, 0.0, 0.0, 0.0
amp_filter = None
filter_sel = 0
filters = ["None", "LP", "HP", "BP"]
filter_peak = 0
filter_freq = 0
filter_int = 50
molego_time=5
osc_a_notes = []
osc_b_notes = []

triangle_wave = 20000 * (np.linspace(0, 1 * np.pi, SAMPLE_SIZE, endpoint=False) / (2 * np.pi) - np.floor(0.5 + np.linspace(0, 1 * np.pi, SAMPLE_SIZE, endpoint=False) / (2 * np.pi)))
triangle_wave_int16 = np.array(triangle_wave, dtype=np.int16)

neg_triangle_wave = -(20000 * (np.linspace(0, 1 * np.pi, SAMPLE_SIZE, endpoint=False) / (2 * np.pi) - np.floor(0.5 + np.linspace(0, 1 * np.pi, SAMPLE_SIZE, endpoint=False) / (2 * np.pi))))
neg_triangle_wave_int16 = np.array(neg_triangle_wave, dtype=np.int16)



                                                                #Don't ask me why, I don't know
vibrato=synthio.LFO(waveform=triangle_wave_int16, rate=molego_time, scale= 3.2897334, offset= 0.0, phase_offset= 0.0, once=True, interpolate=True)

##################################### NOTE ON #############################################

def note_on(notenum, bender):
    osc1_env = synthio.Envelope(attack_time=a1, decay_time=d1, release_time=r1,
                               attack_level=lvl1)
    
    osc2_env = synthio.Envelope(attack_time=a2, decay_time=d2, release_time=r2,
                               attack_level=lvl2)
    
    if filter_sel == 0:
        amp_filter = None
    elif filter_sel == 1:
        amp_filter = synth.low_pass_filter(frequency=filter_freq)
    elif filter_sel == 2:
        amp_filter = synth.high_pass_filter(frequency=filter_freq)
    elif filter_sel == 3:
        amp_filter = synth.band_pass_filter(frequency=filter_freq)

    notes = []
    f = synthio.midi_to_hz(notenum)

    #oscA
    if lvl1 > 0.0:
        for i in range(num_oscs_a):
        #     #  add detuning to oscillators + a bit of random so phases w/ other notes don't perfectly align
            #fr = f * (1 + (osc_detune_a*i) + (random.random()/1000) )
            #detune around the root note
            fr = f * ((1 + osc_detune_a * i * (1 if i % 2 != 0 else -1)) + (random.random()/1000) )
            if fr < 0:
                fr = 0

            vibrato.retrigger()
            #oscA
            osc_a_notes.append( synthio.Note( frequency=fr, envelope=osc1_env, waveform=waveforms[osc1_wave], filter=amp_filter, 
                                        bend=bender
                                        ) )
         
    #oscB
    if lvl2 > 0.0:
        for i in range(num_oscs_b):
            fr = f * (1 + osc_detune_b * i * (1 if i % 2 != 0 else -1) + (random.random()/1000) )
            if fr < 0:
                fr = 0
            vibrato.retrigger()
            #oscB
            osc_b_notes.append( synthio.Note( frequency=fr, envelope=osc2_env, waveform=waveforms[osc2_wave], filter=amp_filter, 
                                        bend=bender
                                        ) )
            
    for note in osc_b_notes:
        notes.append(note)
    for note in osc_a_notes:
        notes.append(note)

    #     print("fr:")
    #notes.append( synthio.Note( frequency=164.82, envelope=amp_env, waveform=waveform) )
    notes_pressed[notenum] = notes

    synth.press(notes)
    

def note_off(notenum):
    notes = notes_pressed.get(notenum, None)
    if notes:
        synth.release(notes)
        del notes_pressed[notenum]
        for note in notes:
            if note in osc_a_notes:
                osc_a_notes.remove(note)
            elif note in osc_b_notes:
                osc_b_notes.remove(note)

def reset_a_env():
    try: #sometimes you turn faster than the dict updates
        for note in osc_a_notes:
            notes_pressed[note][0].envelope = synthio.Envelope(attack_time=a1, decay_time=d1, release_time=r1, attack_level=lvl1)
    except:
        pass

def reset_b_env():
    try:
        for note in osc_b_notes:
            notes_pressed[note][0].envelope = synthio.Envelope(attack_time=a2, decay_time=d2, release_time=r2, attack_level=lvl2)
    except:
        pass

def reset_a_wave():
    try:
        for note in osc_a_notes:
            notes_pressed[note][0].waveform = waveforms[osc1_wave]
    except:
        pass

def reset_b_wave():
    try:
        for note in osc_a_notes:
            notes_pressed[note][0].waveform = waveforms[osc1_wave]
    except:
        pass

def reset_filter():
    try:
        if filter_sel == 0:
            amp_filter = None
        elif filter_sel == 1:
            amp_filter = synth.low_pass_filter(frequency=filter_freq)
        elif filter_sel == 2:
            amp_filter = synth.high_pass_filter(frequency=filter_freq)
        elif filter_sel == 3:
            amp_filter = synth.band_pass_filter(frequency=filter_freq)

        for note in osc_a_notes:
            note.filter=amp_filter
        for note in osc_b_notes:
            note.filter=amp_filter
    except:
        pass

##################################### SETUP #############################################
#which mode is which button and colour
modes = {"0.1":(key_1, (255, 0, 0)), "1.1":(key_2, (0, 0, 255)), "1.2":(key_2, (0, 0, 255)), "1.3":(key_2, (0, 0, 255)), "2.1":(key_3, (0, 255, 0)), "2.2":(key_3, (0, 255, 0))}
mode = 0.1 #Main

arp = False
seq_play = False
sequence = [[], [], [], [], [], [], [], []]
seq_pos = 0
molego_onoff = 0
molego = ["Off", "On"]
molego_note_order = [] #The order of notes played in molego mode

#############
# MAX WIDTH = 11chars
#############
                         #"-Chrd Mode-"
label1 = Label(font, text="-  Main   -",       x=0, y=10)
label2 = Label(font, text=">Key:CMaj",       x=0, y=20)
label3 = Label(font, text="Octave: 5",       x=0, y=30)
label4 = Label(font, text="MoLego:Off",       x=0, y=40)
label5 = Label(font, text="MoTime:5",       x=0, y=50)
label6 = Label(font, text="",       x=0, y=60)
label7 = Label(font, text="",       x=0, y=70)
label8 = Label(font, text="",       x=0, y=80)
note_label = Label(font, text="",       x=0, y=110)
debug_label = Label(font, text="",       x=0, y=120)

changable_labels = [label2, label3, label4, label5, label6, label7, label8, label1]

for label in ([label2, label1, label3, label4, label5, label6, label7, label8, note_label, debug_label]):
    mainscreen.append(label)


encoder_mode = 0 
held_keys = {}
prev_seq_pos = 0

#Lights
if lights:
    leds[key_1] = (255, 255, 255) #main
    leds[key_2] = (0, 0, 255) #adsr
    leds[key_3] = (0, 255, 0) #seq
    leds[key_4] = (255, 128, 0) #arp
    for i in range(8):
        leds[piano_keys[i]] = piano_bg[0] #piano keys
    leds[:] = [[max(i-led_fade,led_min) for i in l] for l in leds]
    leds.show()

##################################### MAIN LOOP #############################################
while True:
    now = ticks_ms()
    #debug_label.text = str(reset_env())
    enc_sw_held = enc_sw_press_millis !=0  and (now - enc_sw_press_millis > 500)    
    if seq_play:
##################################### SEQ HANDLING #############################################

        # LED handling
        if ticks_diff(now, last_led_millis) > 10:  # update every 10 msecs
            last_led_millis = now

            #synth.release_all()
            for note_prev in sequence[seq_pos-1]:
                # if note[2] == "hit": #it was just a hit, turn it off
                #     note_off(note[0])
                #if note[0][:1] in sequence[seq_pos]: 
                held = False
                for note_curr in sequence[seq_pos]:
                    if note_prev[:1] == note_curr[:1]:# if the previous note is also in the current step
                        if note_curr[2] == "hit":
                            note_off(note_prev[0]) #if its a hit, turn it off, else keep it on
                        else:
                            held = True
                if held == False:
                    note_off(note_prev[0]) #otherwise turn it off, regardless of note type
                        
                # else: #for molego only one note is on at a time | Completely broken atm |
                #     for note_curr in sequence[seq_pos]:
                #         if note_prev[:1] == note_curr[:1]:# if the previous note is also in the current step
                #             if note_curr[2] == "hit":
                #                 note_off(note_prev[0])
                #                 #note_on(note_prev[0], bender=synthio.LFO(waveform=triangle_wave_int16, rate=molego_time, scale= 3.2897334*((note_curr[0]-note_prev[0])/12), offset= 0.0, phase_offset= 0.0, once=True, interpolate=True))
                #             else:
                #                 held = True
                #     if held == False: #The molego_note_order is being cleared for some reason
                #         if len(molego_note_order) == 0: #It's the first pass
                #             pass

                #         elif len(molego_note_order) == 1: #It's the last key
                #             note_off(note_prev[0])
                #             molego_note_order.remove(molego_note_order[0])
                        
                #         elif note_prev[0] in molego_note_order and note_prev[0] != molego_note_order[-1]: #There's other keys being pressed that's not the last
                #             molego_note_order.remove(note_prev[0])

                #         elif note_prev[0] == molego_note_order[-1]: #It's the last key (and currently playing) so turn it off an bend down to the 2nd last pressed
                #             old_note = note_prev[0]
                #             new_note = molego_note_order[-2]

                #             note_off(note_prev[0])
                #             note_off(old_note)
                #             old_note = molego_note_order[-1]
                #             note_on(molego_note_order[-1], bender=synthio.LFO(waveform=triangle_wave_int16, rate=molego_time, scale= 3.2897334*((new_note-molego_note_order[-1])/12), offset= 0.0, phase_offset= 0.0, once=True, interpolate=True))
                #            molego_note_order.remove(molego_note_order[-1])



            if seq_pos == 0: #Reset all keys at the start of the sequence
                synth.release_all()

            leds[key_3] = (0, 0, 0)

        # Sequencer playing
        diff = ticks_diff( now, last_step_millis )
        if diff >= step_millis:
            late_millis = ticks_diff( diff, step_millis )  # how much are we late
            last_step_millis = ticks_add( now, -(late_millis//2) ) # attempt to make it up on next step
            # tempo indicator (leds.show() called by LED handler)
            if seq_pos % steps_per_beat == 0: 
                if seq_pos == 0:
                    molego_note_order = []
                #synth.release_all()
                leds[key_3] = (255, 255, 255) #set key3 to tempo
                for note in sequence[seq_pos]:
                    if note[2] == "hit":
                        note_on(note[0], bender=None)

            leds[piano_keys[light_pos]] = (255, 255, 255)
            leds[piano_keys[light_pos-1]] = piano_bg[int(((seq_pos/8) % 4))]
            leds.show()
            seq_pos = (seq_pos + 1) % num_steps
            light_pos = (light_pos + 1) % 8


##################################### KEYPRESS HANDLING #############################################

    key = keys.events.get()
    if key:
        keynum = key.key_number
        
        if keynum in piano_keys: 
            if mode != 2.1:
                padnum = keynum_to_padnum[keynum]
                if key.pressed:
                    if molego_onoff == 0:
                        held_keys[keynum] = 0
                        pads_lit[padnum] = 1
                        note_on(get_note(keynum, scale, octave, "midi"), bender=None)
                        note_label.text = str(get_note(keynum, scale, octave, "text"))
                    else:
                        #no other notes held
                        if len(notes_pressed) == 0 or len(molego_note_order) == 0:
                            held_keys[keynum] = 0
                            pads_lit[padnum] = 1
                            note_on(get_note(keynum, scale, octave, "midi"), bender=None)
                            note_label.text = str(get_note(keynum, scale, octave, "text"))
                        else:
                            
                            new_note = get_note(keynum, scale, octave, "midi")

                            for note in osc_a_notes:
                                note.bend = synthio.LFO(waveform=triangle_wave_int16, rate=molego_time, scale= 3.2897334*(((new_note-molego_note_order[-1]) )/12), offset= (molego_note_order[-1]-molego_note_order[0])/12, phase_offset= 0.0, once=True, interpolate=True)

                            for note in osc_b_notes:
                                note.bend = synthio.LFO(waveform=triangle_wave_int16, rate=molego_time, scale= 3.2897334*(((new_note-molego_note_order[-1]) )/12), offset= (molego_note_order[-1]-molego_note_order[0])/12, phase_offset= 0.0, once=True, interpolate=True)
                        
                        molego_note_order.append(get_note(keynum, scale, octave, "midi"))
                        

                if key.released:
                    if molego_onoff == 0:
                        note_off(get_note(keynum, scale, octave, "midi"))
                    else:
                        if len(molego_note_order) == 1: #It's the last key
                            note_off(next(iter(notes_pressed)))
                            molego_note_order.remove(molego_note_order[0])
                        
                        elif get_note(keynum, scale, octave, "midi") in molego_note_order and get_note(keynum, scale, octave, "midi") != molego_note_order[-1]: #There's other keys being pressed that's not the last
                            molego_note_order.remove(get_note(keynum, scale, octave, "midi"))

                        elif get_note(keynum, scale, octave, "midi") == molego_note_order[-1]: #It's the last key (and currently playing) so turn it off an bend down to the 2nd last pressed
                            old_note = next(iter(notes_pressed))
                            new_note = molego_note_order[-2]
                            for note in osc_a_notes:
                                note.bend = synthio.LFO(waveform=triangle_wave_int16, rate=molego_time, scale= 3.2897334*((new_note-molego_note_order[-1])/12), offset= (molego_note_order[-1]-molego_note_order[0])/12, phase_offset= 0.0, once=True, interpolate=True)

                            for note in osc_b_notes:
                                note.bend = synthio.LFO(waveform=triangle_wave_int16, rate=molego_time, scale= 3.2897334*((new_note-molego_note_order[-1])/12), offset= (molego_note_order[-1]-molego_note_order[0])/12, phase_offset= 0.0, once=True, interpolate=True)

                            molego_note_order.remove(molego_note_order[-1])


            else:
                padnum = keynum_to_padnum[keynum]
                if key.pressed:
                    if encoder_mode == 0: #add mode
                        if (get_note(keynum, scale, octave, "midi"), keynum, "hit") in sequence[seq_pos]:
                            #Change hit to hold
                            sequence[seq_pos].remove((get_note(keynum, scale, octave, "midi"), keynum, "hit"))
                            sequence[seq_pos].append((get_note(keynum, scale, octave, "midi"), keynum, "hold"))
                            
                            if piano_keys[light_pos] == keynum:
                                leds[piano_keys[light_pos]] = (60, 60, 255)
                            else:
                                leds[keynum] = (0, 0, 255) #set new key

                        elif (get_note(keynum, scale, octave, "midi"), keynum, "hold") in sequence[seq_pos]:
                            #Delete
                            sequence[seq_pos].remove((get_note(keynum, scale, octave, "midi",), keynum, "hold"))
                            
                            if piano_keys[light_pos] == keynum:
                                leds[piano_keys[light_pos]] = (255, 255, 255)
                            else:
                                leds[keynum] = piano_bg[int(((seq_pos/8) % 4))] #set new key
                        
                        else: #add it
                            sequence[seq_pos].append((get_note(keynum, scale, octave, "midi"), keynum, "hit"))
                            note_label.text = str(get_note(keynum, scale, octave, "text"))

                            if piano_keys[light_pos] == keynum:
                                leds[piano_keys[light_pos]] = (255, 60, 60)
                            else:
                                leds[keynum] = (255, 0, 0) #set new key

                        leds.show()


        elif key.pressed:
            if keynum == key_1: #main 
                if mode != 0.1:
                    leds[modes[str(mode)][0]] = modes[str(mode)][1]
                    leds[key_1] = (255, 255, 255)
                    if not seq_play:
                        for i in range(8):
                            leds[piano_keys[i]] = (255, 0, 255) #piano keys
                    leds.show()
                    mode = 0.1
                    encoder_mode = 0
                    label1.text="-  Main   -"
                    label2.text=">Key:"+(num2note[scale])+"Maj"
                    #label3.text = "Wave:"+waveform_name[wave]
                    label3.text = "Octave: "+str(octave)
                    label4.text = "MoLego:"+molego[molego_onoff]
                    label5.text = "MoTime:"+str(molego_time)
                    label6.text=""
                    label7.text=""
                    label8.text=""
                    note_label.text=""
                    debug_label.text=""

                elif mode == 0.1:
                    label1.text="-  Main   -"
                    label2.text="ALL"
                    label3.text = "NOTES"
                    label4.text="OFF"
                    label5.text=""
                    label6.text=""
                    label7.text=""
                    label8.text=""
                    note_label.text=""
                    debug_label.text=""
                    synth.release_all()
                    molego_note_order = []

                    time.sleep(0.5)

                    leds[modes[str(mode)][0]] = modes[str(mode)][1]
                    leds[key_1] = (255, 255, 255)
                    if not seq_play:
                        for i in range(8):
                            leds[piano_keys[i]] = (255, 0, 255) #piano keys
                    leds.show()
                    mode = 0.1
                    encoder_mode = 0
                    label1.text="-  Main   -"
                    label2.text=">Key:"+(num2note[scale])+"Maj"
                    #label3.text = "Wave:"+waveform_name[wave]
                    label3.text = "Octave: "+str(octave)
                    label4.text = "MoLego:"+molego[molego_onoff]
                    label5.text = "MoTime:"+str(molego_time)
                    label6.text=""
                    label7.text=""
                    label8.text=""
                    note_label.text=""
                    debug_label.text=""

            elif keynum == key_2: #adsr
                if mode != 1.1 and mode != 1.2:
                    leds[modes[str(mode)][0]] = modes[str(mode)][1]
                    leds[key_2] = (255, 255, 255)
                    if not seq_play:
                        for i in range(8):
                            leds[piano_keys[i]] = (255, 0, 255) #piano keys
                    leds.show()
                    mode = 1.1
                    encoder_mode = 0
                   #label1.text="-  ADSR   -"
                    label1.text="-  OSC A  -"
                    label2.text=">Level:"+str(lvl1)
                    label3.text="Atk:"+str(a1)
                    label4.text="Dcy:"+str(d1)
                    label5.text="Rls:"+str(r1)
                    label6.text="Wave:"+str(waveform_name[osc1_wave])
                    label7.text="Voices:"+str(num_oscs_a)
                    label8.text="Dtune:"+str(osc_detune_a)
                    note_label.text=""
                    debug_label.text=""

                elif mode == 1.1:   #Osc 2
                    mode = 1.2
                    encoder_mode = 0
                    label1.text="-  OSC B  -"
                    label2.text=">Level:"+str(lvl2)
                    label3.text="Atk:"+str(a2)
                    label4.text="Dcy:"+str(d2)
                    label5.text="Rls:"+str(r2)
                    label6.text="Wave:"+str(waveform_name[osc2_wave])
                    label7.text="Voices:"+str(num_oscs_b)
                    label8.text="Dtune:"+str(osc_detune_b)
                    note_label.text=""
                    debug_label.text=""
                
                elif mode == 1.2:   #Filter
                    mode = 1.3
                    # encoder_mode = 0
                    # label1.text="- MOLEGO  -"
                    # label2.text=">"+molego[molego_onoff]
                    # label3.text = "Time:"+str(molego_time)
                    # label4.text=""
                    # label5.text=""
                    # note_label.text=""
                    # debug_label.text=""
                    encoder_mode = 0
                    label1.text="- FILTER  -"
                    label2.text=">Type:"+filters[filter_sel]
                    label3.text = "Freq:"+str(filter_freq)
                    label4.text="int:"+str(filter_int)
                    label5.text=""
                    label6.text=""
                    label7.text=""
                    label8.text=""
                    note_label.text=""
                    debug_label.text=""

            elif keynum == key_3: #seq
                if mode != 2.1 and seq_play == False or mode == 2.2: #step seq
                    seq_play = False
                    synth.release_all()
                    molego_note_order = []
                    
                    #if mode == 2.2:
                        #seq_pos = seq_pos-1
                        #seq_pos = 0
                    for i in range(8):
                        leds[piano_keys[i]] = piano_bg[int(((seq_pos/8) % 4))] #piano keys
                    leds[piano_keys[light_pos]] = (255, 255, 255)

                    for note in sequence[seq_pos]:
                        if piano_keys[light_pos] == note[1]:
                            leds[piano_keys[light_pos]] = (255, 60, 60)
                        else:
                            leds[note[1]] = (255, 0, 0) #set new key

                    leds[modes[str(mode)][0]] = modes[str(mode)][1]
                    leds[key_3] = (255, 255, 255)
                    leds.show()
                    mode = 2.1
                    encoder_mode = 0
                    label1.text="- SEQ Step-"
                    label2.text=">Add"
                    label3.text = "Del"
                    label4.text=""
                    label5.text=""
                    label6.text=""
                    label7.text=""
                    label8.text=""
                    note_label.text=""
                    debug_label.text=""

                elif mode == 2.1 or seq_play == True: #seq play
                    leds[modes[str(mode)][0]] = modes[str(mode)][1]
                    seq_play = True
                    mode = 2.2
                    encoder_mode = 0
                    label1.text="- SEQ Play-"
                    label2.text=">"+str(num_steps)+" Steps"
                    label3.text = ""
                    label4.text=""
                    label5.text=""
                    label6.text=""
                    label7.text=""
                    label8.text=""
                    note_label.text=""
                    debug_label.text=""


##################################### ENCODER PUSH HANDLING #############################################
    enc_sw = encoder_switch.events.get()
    if enc_sw:
        if enc_sw.pressed:
            enc_sw_press_millis = now
        if enc_sw.released:
            if not enc_sw_held:  # press & release not press-hold
                if mode == 2.1: #2 options
                    changable_labels[encoder_mode].text = changable_labels[encoder_mode].text[1:]
                    encoder_mode = (encoder_mode + 1) % 2
                    changable_labels[encoder_mode].text = ">"+changable_labels[encoder_mode].text

                elif mode == 1.3 : #3 options
                    changable_labels[encoder_mode].text = changable_labels[encoder_mode].text[1:]
                    encoder_mode = (encoder_mode + 1) % 3
                    changable_labels[encoder_mode].text = ">"+changable_labels[encoder_mode].text

                elif mode == 0.1:
                    changable_labels[encoder_mode].text = changable_labels[encoder_mode].text[1:]
                    encoder_mode = (encoder_mode + 1) % 4
                    changable_labels[encoder_mode].text = ">"+changable_labels[encoder_mode].text

                elif mode == 1.1 or mode == 1.2: #main page 1 -> 4 options
                    changable_labels[encoder_mode].text = changable_labels[encoder_mode].text[1:]
                    encoder_mode = (encoder_mode + 1) % 7
                    changable_labels[encoder_mode].text = ">"+changable_labels[encoder_mode].text
                
                if mode == 2.1 and encoder_mode == 1: #clear current pattern
                    for i in range(8):
                        leds[piano_keys[i]] = piano_bg[int(((seq_pos/8) % 4))] #piano keys #reset all keys
                    leds[piano_keys[light_pos]] = (255, 255, 255) #set new key
                    sequence[seq_pos] = [] #reset pattern
                    leds.show()

            enc_sw_press_millis = 0

##################################### ENCODER TURN HANDLING #############################################
    encoder_val = encoder.position
    if encoder_val != encoder_val_last:
        if mode == 0.1: #Main mode page 1
            if encoder_mode == 0:  # mode 1 == change key
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if scale+encoder_delta > 11:
                    encoder_delta -= 12
                elif scale+encoder_delta < 0:
                    encoder_delta += 12
                scale += encoder_delta
                label2.text = ">Key:"+(num2note[scale])+"Maj"

            elif encoder_mode == 1:  # mode 2 == change octave
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if octave+encoder_delta > 9:
                    encoder_delta -= 10
                elif octave+encoder_delta < -1:
                    encoder_delta += 10
                octave += encoder_delta
                label3.text = ">Octave: "+str(octave)

            elif encoder_mode == 2:  # mode 3 == change molego on/off
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if molego_onoff+encoder_delta > 1:
                    encoder_delta -= 2
                elif molego_onoff+encoder_delta < 0:
                    encoder_delta += 2
                molego_onoff += encoder_delta
                label4.text=">MoLego:"+molego[molego_onoff]

            elif encoder_mode == 3:  # change molego time
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                molego_time += encoder_delta
                label5.text = ">MoTime:"+str(molego_time)

        # label1.text="-OSCA OPTS-"
        # label2.text=">Level:"+str(lvl1)
        # label3.text="Atk:"+str(a1)
        # label4.text="Dcy:"+str(d1)
        # label5.text="Rls:"+str(r1)
        # label6.text="Wave:"+str(waveform_name[osc1_wave])
        # label7.text="Voices:"+str(num_oscs_a)
        # label8.text="Dtune:"+str(osc_detune_a)

        elif mode == 1.1: #Osc A
            if encoder_mode == 0:  # mode 1 == change lvl
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if lvl1 + encoder_delta*0.1 >= 0 and lvl1 + encoder_delta*0.1 <= 1:
                    lvl1 += encoder_delta*0.1
                    lvl1 = round(lvl1, 1)
                label2.text=">Level:"+str(lvl1)
                reset_a_env()
            

            elif encoder_mode == 1:  # change atk
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if a1 + encoder_delta*0.01 >= 0 and a1 + encoder_delta*0.01 <= 1:
                    a1 += encoder_delta*0.01
                    a1 = round(a1, 2)
                label3.text=">Atk:"+str(a1)
                reset_a_env()
                

            elif encoder_mode == 2:  # change decay
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if d1 + encoder_delta*0.01 >= 0 and d1 + encoder_delta*0.01 <= 1:
                    d1 += encoder_delta*0.01
                    d1 = round(d1, 2)
                label4.text=">Dcy:"+str(d1)
                reset_a_env()
                

            elif encoder_mode == 3:  # change release
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if r1 + encoder_delta*0.01 >= 0 and r1 + encoder_delta*0.01 <= 1:
                    r1 += encoder_delta*0.01
                    r1 = round(r1, 2)
                label5.text=">Rls:"+str(r1)
                reset_a_env()

            elif encoder_mode == 4:  # change wave
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if osc1_wave+encoder_delta > 4:
                    encoder_delta -= 5
                elif osc1_wave+encoder_delta < 0:
                    encoder_delta += 5
                osc1_wave += encoder_delta
                label6.text = ">Wave:"+waveform_name[osc1_wave]
                reset_a_wave()

            elif encoder_mode == 5:  # change voices
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if num_oscs_a + encoder_delta >= 1 and num_oscs_a + encoder_delta <= 12:
                    num_oscs_a += encoder_delta
                label7.text=">Voices:"+str(num_oscs_a)

            elif encoder_mode == 6:  # change release
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if osc_detune_a + encoder_delta*0.01 >= 0 and osc_detune_a + encoder_delta*0.01 <= 1:
                    osc_detune_a += encoder_delta*0.01
                    osc_detune_a = round(osc_detune_a, 2)
                label8.text=">Dtune:"+str(osc_detune_a)

        elif mode == 1.2: #Osc B
            if encoder_mode == 0:  # mode 1 == change lvl
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if lvl2 + encoder_delta*0.1 >= 0 and lvl2 + encoder_delta*0.1 <= 1:
                    lvl2 += encoder_delta*0.1
                    lvl2 = round(lvl2, 1)
                label2.text=">Level:"+str(lvl2)
                reset_b_env()
            

            elif encoder_mode == 1:  # change atk
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if a2 + encoder_delta*0.01 >= 0 and a2 + encoder_delta*0.01 <= 1:
                    a2 += encoder_delta*0.01
                    a2 = round(a2, 2)
                label3.text=">Atk:"+str(a2)
                reset_b_env()
                

            elif encoder_mode == 2:  # change decay
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if d2 + encoder_delta*0.01 >= 0 and d2 + encoder_delta*0.01 <= 1:
                    d2 += encoder_delta*0.01
                    d2 = round(d2, 2)
                label4.text=">Dcy:"+str(d2)
                reset_b_env()
                

            elif encoder_mode == 3:  # change release
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if r2 + encoder_delta*0.01 >= 0 and r2 + encoder_delta*0.01 <= 1:
                    r2 += encoder_delta*0.01
                    r2 = round(r2, 2)
                label5.text=">Rls:"+str(r2)
                reset_b_env()

            elif encoder_mode == 4:  # change wave
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if osc2_wave+encoder_delta > 4:
                    encoder_delta -= 5
                elif osc2_wave+encoder_delta < 0:
                    encoder_delta += 5
                osc2_wave += encoder_delta
                label6.text = ">Wave:"+waveform_name[osc2_wave]
                reset_b_wave()

            elif encoder_mode == 5:  # change voices
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if num_oscs_b + encoder_delta >= 1 and num_oscs_b + encoder_delta <= 12:
                    num_oscs_b += encoder_delta
                label7.text=">Voices:"+str(num_oscs_b)

            elif encoder_mode == 6:  # change release
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if osc_detune_b + encoder_delta*0.01 >= 0 and osc_detune_b + encoder_delta*0.01 <= 1:
                    osc_detune_b += encoder_delta*0.01
                    osc_detune_b = round(osc_detune_b, 2)
                label8.text=">Dtune:"+str(osc_detune_b)
                

        elif mode == 1.3: #adsr mode page 2
            if encoder_mode == 0:  # change filter
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if filter_sel+encoder_delta > 3:
                    encoder_delta -= 4
                elif filter_sel+encoder_delta < 0:
                    encoder_delta += 4
                filter_sel += encoder_delta
                label2.text=">Type:"+filters[filter_sel]
                reset_filter()

            elif encoder_mode == 1:  # change freq
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if filter_freq + encoder_delta*filter_int >= 1: #and filter_freq + encoder_delta*100 <= 16000:
                    filter_freq += encoder_delta*filter_int
                label3.text = ">Freq:"+str(filter_freq)
                reset_filter()

            elif encoder_mode == 2:  # change freq interval
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if filter_int + encoder_delta*50 >= 0: #and filter_freq + encoder_delta*100 <= 16000:
                    filter_int += encoder_delta*50
                label4.text = ">int:"+str(filter_int)           

            

#implement steps first
        elif mode == 2.1: #seq step mode
            if encoder_mode == 0:  # add mode
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if seq_pos+encoder_delta > num_steps-1:
                    encoder_delta -= num_steps
                elif seq_pos+encoder_delta < 0:
                    encoder_delta += num_steps


                seq_pos += encoder_delta
                light_pos += encoder_delta
                light_pos = (light_pos) % 8

                for i in range(8):
                    leds[piano_keys[i]] = piano_bg[int(((seq_pos/8) % 4))] #piano keys #reset all keys

                leds[piano_keys[light_pos]] = (255, 255, 255) #set new key
                for note in sequence[seq_pos]:
                    if piano_keys[light_pos] == note[1]:
                        if note[2] == "hit":
                            leds[piano_keys[light_pos]] = (255, 60, 60)
                        else:
                            leds[piano_keys[light_pos]] = (60, 60, 255)
                    else:
                        if note[2] == "hit":
                            leds[note[1]] = (255, 0, 0) #set new key
                        else:
                            leds[note[1]] = (0, 0, 255)
                        

                leds.show()
                label2.text=">Add"

            elif encoder_mode == 1:  # delete mode
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if seq_pos+encoder_delta > num_steps-1:
                    encoder_delta -= num_steps
                elif seq_pos+encoder_delta < 0:
                    encoder_delta += num_steps


                seq_pos += encoder_delta
                light_pos += encoder_delta
                light_pos = (light_pos) % 8
                for i in range(8):
                    leds[piano_keys[i]] = piano_bg[int(((seq_pos/8) % 4))] #piano keys #reset all keys


                leds[piano_keys[light_pos]] = (255, 255, 255) #set new key
                sequence[seq_pos] = [] #reset pattern

                leds.show()
                label3.text=">Del"
        
        elif mode == 2.2: #seq play mode
            if encoder_mode == 0:  # mode 1 == change seq length
                encoder_delta = (encoder_val - encoder_val_last)
                encoder_val_last = encoder_val
                if encoder_delta < 0:
                    num_steps *= 0.5
                    num_steps = int(str(num_steps)[:-2])
                else:
                    num_steps *= 2
                if num_steps < 8:
                    num_steps = 8
                while len(sequence) < num_steps:
                    sequence.append([])

                leds.show()
                label2.text=">"+str(num_steps)+" Steps"
