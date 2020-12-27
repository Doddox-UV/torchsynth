"""
Synth modules.

    TODO    :   - VCA is slow. Blocks? Not sure what's best for tensors.
                - Convert operations to tensors, obvs.
"""

import copy

import numpy as np
from scipy.signal import resample

from ddspdrum.defaults import CONTROL_RATE, SAMPLE_RATE


class SynthModule:
    """
    Base class for synthesis modules. Mostly helper functions for the moment.
    """

    def __init__(
        self, sample_rate: int = SAMPLE_RATE, control_rate: int = CONTROL_RATE
    ):
        self.sample_rate = SAMPLE_RATE
        self.control_rate = CONTROL_RATE

    def control_to_sample_rate(self, control: np.array) -> np.array:
        """
        Resample a control signal to the sample rate
        TODO: One thing I worry about with all these casts to
        ints back and forth between sample and control rate is
        off-by-one errors.
        I'm beginning to believe we should standardize as much
        as possible on nsamples in most places instead of
        float duration in seconds, and just convert to ncontrol
        when needed..
        """
        # Right now it appears that all signals are 1d, but later
        # we'll probably convert things to 2d: instance x signal
        assert control.ndim == 1
        nsamples = int(round(len(control) * self.sample_rate / self.control_rate))
        return resample(control, nsamples)

    def fix_length(self, signal: np.array, length: int) -> np.array:
        # Right now it appears that all signals are 1d, but later
        # we'll probably convert things to 2d: instance x signal
        assert signal.ndim == 1
        if len(signal) < length:
            signal = np.pad(signal, [0, length - len(signal)])
        elif len(signal) > length:
            signal = signal[:length]
        assert signal.shape == (length,)
        return signal


class ADSR(SynthModule):
    """
    Envelope class for building a control rate ADSR signal
    """

    def __init__(
        self,
        a: float = 0.25,
        d: float = 0.25,
        s: float = 0.5,
        r: float = 0.5,
        alpha: float = 3.0,
        sample_rate: int = SAMPLE_RATE,
        control_rate: int = CONTROL_RATE,
    ):
        """
        Parameters
        ----------
        a                   :   attack time (sec), >= 0
        d                   :   decay time (sec), >= 0
        s                   :   sustain amplitude between 0-1. The only part of
                                ADSR that (confusingly, by convention) is not in
                                the seconds domain.
        r                   :   release time (sec), >= 0
        alpha               :   envelope curve, >= 0. 1 is linear, >1 is exponential.
        """
        super().__init__(sample_rate=sample_rate, control_rate=control_rate)
        assert alpha >= 0
        self.alpha = alpha

        assert s >= 0 and s <= 1
        self.s = s

        assert a >= 0
        self.a = a

        assert d >= 0
        self.d = d

        assert r >= 0
        self.r = r

    def __call__(self, sustain_duration):
        """
        Play, for some duration in seconds.
        """
        assert sustain_duration >= 0
        return np.concatenate(
            (
                # Is this the DSP-way of thinking about it, (note on and note off)
                # or can we change this just to self.attack, self.decay, etc.
                self.note_on,
                # This makes me believe that duration should maybe be a class value
                # defined in __init__. How common is it to create a module and
                # with fixed settings and use it several times with different
                # duration?
                self.sustain(sustain_duration),
                self.note_off,
            )
        )

    @property
    def attack(self):
        return self._ramp(self.a)

    @property
    def decay(self):
        # TODO: This is a bit obtuse and would be great to explain
        return self._ramp(self.d)[::-1] * (1 - self.s) + self.s

    def sustain(self, duration):
        return np.full(round(int(duration * CONTROL_RATE)), fill_value=self.s)

    @property
    def release(self):
        # TODO: This is a bit obtuse and would be great to explain
        return self._ramp(self.r)[::-1] * self.s

    @property
    def note_on(self):
        return np.append(self.attack, self.decay)

    @property
    def note_off(self):
        return self.release

    def _ramp(self, duration: float):
        """
        Create a ramp function for a certain duration in seconds,
        applying the envelope curve and returning it in control rate.
        """
        t = np.linspace(
            0, duration, int(round(duration * self.control_rate)), endpoint=False
        )
        return (t / duration) ** self.alpha

    def __str__(self):
        return (
            f"ADRS(a={self.a}, d={self.d}, s={self.s}, r={self.r}, alpha={self.alpha})"
        )


class VCO(SynthModule):
    """
    Voltage controlled oscillator. Accepts control rate instantaneous frequency, outputs audio.

    TODO: more than just cosine.

    Examples
    --------

    >>> myVCO = VCO()
    >>> two_8ve_chirp = myVCO(np.linspace(440, 1760, 1000))
    """

    def __init__(
        self, sample_rate: int = SAMPLE_RATE, control_rate: int = CONTROL_RATE
    ):
        super().__init__(sample_rate=sample_rate, control_rate=control_rate)
        self.__f0 = []
        self.__phase = 0

    def __call__(self, f0control: np.array, phase: float):
        # TODO: Are there expected ranges on phase?
        # Does it matter? Can we just assert preconditions here?
        phase = phase % (2 * np.pi)

        nyquist = self.sample_rate // 2
        # I'm still a bit concerned about using clamp,
	    # because we lose the gradients from it, but let's worry
        # about that later.
        clampedf0control = np.clamp(f0control, 0, nyquist)

        # What is arg and can we give it a better name?
        arg = self.to_arg(clampedf0signal) + phase
        # Why do we do a `set_phase` here at the end?
        # If this is really some sort of recurrent state then maybe
        # we return it and pass it back in?
        #self.set_phase(arg[-1])
        return np.cos(arg)

    def to_arg(self, f0control: np.array):
        # Can we describe what this does and give it a better name?
        # I see that it upsamples from control to sample rate.
        # Then it does a cumulative sum?? So it's to modulate the
        # pitch over time?
        up_sampled = self.control_to_sample_rate(f0)
        return np.cumsum(2 * np.pi * up_sampled / SAMPLE_RATE)

class VCA(SynthModule):
    """
    Voltage controlled amplifier. Shapes amplitude of audio rate signal with control rate level.
    """

    def __init__(
        self, sample_rate: int = SAMPLE_RATE, control_rate: int = CONTROL_RATE
    ):
        super().__init__(sample_rate=sample_rate, control_rate=control_rate)
        self.__envelope = np.array([])
        self.__audio = np.array([])

    def __call__(self, envelope, audio):
        self.set_envelope(envelope)
        self.set_audio(audio)
        return self.play()

    def get_envelope(self):
        return self.__envelope

    def set_envelope(self, envelope):
        envelope = np.clip(envelope, 0, 1)
        self.__envelope = envelope

    def get_audio(self):
        return self.__audio

    def set_audio(self, audio):
        audio = np.clip(audio, -1, 1)
        self.__audio = audio

    def play(self):
        amp = self.control_to_sample_rate(self.get_envelope())
        signal = self.fix_length(self.get_audio(), len(amp))
        return amp * signal
