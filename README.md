# CloudTrail Sonifier

**tail -f for your ears** — Listen to your AWS infrastructure in real time.

CloudTrail Sonifier polls AWS CloudTrail for API events and renders them as
music. Events are grouped into one-second time buckets and played as chords,
so you hear the density and character of your cloud activity at a glance.
Busy seconds produce thick, rich chords. Quiet seconds produce thin, sustained
tones. Errors cut through with dissonance. You develop an intuitive sense of
"normal" and immediately notice when something sounds wrong.

Inspired by [JFugue](http://www.jfugue.org/) and
[Log4JFugue](https://log4jfugue.org/), which won the 2010 Duke's Choice Award
at JavaOne for the Most Innovative Use of Java.

## How It Works

| Cloud Signal         | Musical Mapping                                           |
|----------------------|-----------------------------------------------------------|
| AWS Service          | Instrument / waveform (EC2=sine, S3=triangle, IAM=square) |
| API Action           | Pitch (reads=low, creates=mid, updates=high, deletes=highest) |
| Repeated events      | Louder (velocity scales with occurrence count)            |
| Error events         | Dissonant intervals + noise burst + extended duration     |
| Source IP            | Stereo pan position (hashed)                              |
| Event density        | Chord thickness and playback pace                         |

Chords are stretched to fill the entire poll interval, producing a continuous
ambient stream rather than bursts of sound followed by silence.

## Requirements

- Python 3.10+
- An AWS account with CloudTrail enabled (it is by default)
- AWS credentials configured with at least `cloudtrail:LookupEvents` permission

## Installation

### 1. Install Python dependencies

The simplest setup uses the sounddevice backend, which synthesizes audio
directly through your speakers with no MIDI routing required:

```bash
pip install boto3 sounddevice numpy
```

### 2. Configure AWS credentials

Any standard method works:

```bash
# Option A: AWS CLI (interactive)
aws configure

# Option B: Environment variables
export AWS_ACCESS_KEY_ID="AKIA..."
export AWS_SECRET_ACCESS_KEY="..."
export AWS_DEFAULT_REGION="us-east-1"

# Option C: SSO / IAM Identity Center
aws sso login --profile my-profile
export AWS_PROFILE=my-profile
```

### 3. Minimum IAM policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "cloudtrail:LookupEvents",
      "Resource": "*"
    }
  ]
}
```

## Quick Start

```bash
# Verify audio works (plays a test scale, then exits)
python cloudtrail_sonifier.py --test

# Run with defaults (us-east-1, 60s poll interval, 1s chords)
python cloudtrail_sonifier.py

# Filter to specific services
python cloudtrail_sonifier.py --services s3 ec2 iam lambda

# Shorter chords, faster feel
python cloudtrail_sonifier.py --chord-duration 0.5

# Longer, more ambient chords
python cloudtrail_sonifier.py --chord-duration 1.5

# Different region
python cloudtrail_sonifier.py --region eu-west-1

# Print event mappings without audio
python cloudtrail_sonifier.py --dry-run
```

## Command Line Options

| Option              | Default       | Description                                    |
|---------------------|---------------|------------------------------------------------|
| `--region`          | `us-east-1`   | AWS region to poll                             |
| `--interval`        | `60`           | Seconds between polls                          |
| `--chord-duration`  | `1.0`          | Base chord length in seconds                   |
| `--services`        | all            | Space-separated list of services to include    |
| `--backend`         | `auto`         | Audio backend: `fluidsynth`, `sounddevice`, `midi`, `auto` |
| `--soundfont`       | auto-detected  | Path to a `.sf2` SoundFont file (FluidSynth)  |
| `--virtual-port`    | none           | Virtual MIDI port name (mido backend)          |
| `--test`            | off            | Play a test scale to verify audio, then exit   |
| `--dry-run`         | off            | Print event mappings without playing audio     |

## Audio Backends

The sonifier supports three audio backends and will auto-detect the best
available option in this order:

### 1. FluidSynth (richest sound)

Uses General MIDI instruments via a SoundFont file. Each AWS service gets its
own instrument: EC2 is piano, S3 is marimba, IAM is trumpet, Lambda is a synth
lead, and so on.

```bash
pip install boto3 pyfluidsynth

# macOS
brew install fluidsynth

# Linux
sudo apt install fluidsynth fluid-soundfont-gm

# Download a SoundFont if needed
curl -L -o ~/FluidR3_GM.sf2 "https://keymusician01.s3.amazonaws.com/FluidR3_GM.sf2"

python cloudtrail_sonifier.py --backend fluidsynth --soundfont ~/FluidR3_GM.sf2
```

### 2. sounddevice (zero config, recommended for getting started)

Synthesizes sine, triangle, square, and sawtooth waveforms directly. No MIDI,
no SoundFonts, no external software. Each AWS service is mapped to a different
waveform shape so you can still distinguish services by timbre.

```bash
pip install boto3 sounddevice numpy
python cloudtrail_sonifier.py --backend sounddevice
```

### 3. mido (DAW routing)

Sends MIDI messages to an external synthesizer or DAW. Useful if you want to
use your own instruments in GarageBand, Ableton, Logic, etc.

```bash
pip install boto3 mido python-rtmidi
python cloudtrail_sonifier.py --backend midi --virtual-port "CloudTrail Music"
# Then select "CloudTrail Music" as a MIDI input in your DAW
```

On macOS, the IAC MIDI driver works out of the box. On Windows, use
[loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html) as a virtual
MIDI cable.

## Understanding the Output

```
  ▶ 50 events → 20 chord(s)
  chord   1/20  │   2 events │ E4(kms) + D3(dynamodb)
              │ density  │ ██
  chord   6/20  │  17 events │ C3×11(ecr) + A#4×6(ecr)
              │ density  │ █████████████████
  chord  12/20  │   4 events │ E3(svc) + D3(elb) + A#3(sts) + C3(svc) ✖ ERRORS
              │ density  │ ████
              │ ✖ ERROR  │ s3.GetObject: AccessDenied — Access Denied
```

Each chord line shows the number of events, the unique pitches with their
occurrence counts and originating services, and a density bar. Error chords are
flagged with ✖ and each error is printed with its service, API call, error code,
and full error message.

## Important Notes

**CloudTrail delivery delay.** Events take 5-15 minutes to appear in the
`LookupEvents` API after they occur. The sonifier uses a rolling 20-minute
lookback window and deduplicates by event ID, so new events are picked up as
soon as CloudTrail delivers them.

**Rate limiting.** The CloudTrail `LookupEvents` API has a low rate limit. The
default 60-second poll interval avoids throttling under normal conditions. If
throttled, the sonifier backs off automatically (starting at 30 seconds,
doubling up to 5 minutes) and resets once a successful poll completes.

**Single API call per poll.** Each poll makes exactly one `LookupEvents` call
(no pagination) to stay well within rate limits. This caps each poll at 50
events. Very busy accounts may not capture every event, but the musical
representation will still accurately reflect activity patterns.

## Troubleshooting

| Problem                        | Solution                                           |
|--------------------------------|----------------------------------------------------|
| `Unable to locate credentials` | Run `aws configure` or set `AWS_PROFILE`           |
| `--test` works but normal mode is silent | Events haven't been delivered yet; wait 10-15 min |
| `ThrottlingException`          | Increase `--interval` (try 90 or 120)              |
| No sound, no errors            | Check your system volume; try `--test` first       |
| `No audio backend available`   | `pip install sounddevice numpy`                    |

## License

MIT
