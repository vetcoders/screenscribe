var DEBUG = (typeof window !== 'undefined' && window.DEBUG !== undefined)
    ? window.DEBUG
    : (typeof window !== 'undefined' && window.location?.search?.includes('debug=1'));
if (typeof window !== 'undefined') window.DEBUG = DEBUG;

class ScreenScribePlayer {
    constructor() {
        this.video = document.getElementById('videoPlayer');
        this.subtitleDisplay = document.getElementById('currentSubtitle');
        this.subtitleList = document.getElementById('subtitleList');
        this.searchBox = document.getElementById('subtitleSearch');
        this.transcriptPanel = document.getElementById('transcriptPanel');
        this.playPauseBtn = document.getElementById('playPauseBtn');
        this.secondBackBtn = document.getElementById('secondBackBtn');
        this.secondForwardBtn = document.getElementById('secondForwardBtn');
        this.stepBackBtn = document.getElementById('stepBackBtn');
        this.stepForwardBtn = document.getElementById('stepForwardBtn');
        this.jumpBackBtn = document.getElementById('jumpBackBtn');
        this.jumpForwardBtn = document.getElementById('jumpForwardBtn');
        this.captureFrameBtn = document.getElementById('captureFrameBtn');
        this.frameSweep = document.getElementById('frameSweep');
        this.currentTimeLabel = document.getElementById('currentTimeLabel');

        this.segments = window.TRANSCRIPT_SEGMENTS || [];
        this.hasTranscriptSource = this.segments.length > 0;
        this.currentSegmentId = null;
        this.frameStepSeconds = 1 / 30;
        this.isDraggingSweep = false;

        this.init();
    }

    init() {
        if (!this.video) return;

        this.video.addEventListener('timeupdate', () => this.onTimeUpdate());
        this.video.addEventListener('loadedmetadata', () => this.onMetadataLoaded());
        this.video.addEventListener('play', () => this.updatePlayPauseButton());
        this.video.addEventListener('pause', () => this.updatePlayPauseButton());
        this.video.addEventListener('error', () => this.showVideoPlaybackError());

        this.video.addEventListener('click', () => {
            this.video.paused ? this.safePlay() : this.video.pause();
        });

        this.configureTranscriptSource();
        this.bindPrecisionControls();
        this.renderSubtitleList(this.segments);

        if (this.searchBox) {
            this.searchBox.addEventListener('input', (e) => {
                const query = e.target.value.toLowerCase();
                const filtered = this.segments.filter(s =>
                    s.text.toLowerCase().includes(query)
                );
                this.renderSubtitleList(filtered);
            });
        }

        document.addEventListener('keydown', (e) => {
            if (this.isNativeKeyTarget(e.target)) return;

            if (e.code === 'Space') {
                e.preventDefault();
                this.video.paused ? this.safePlay() : this.video.pause();
            }
            if (e.code === 'ArrowLeft') {
                e.preventDefault();
                this.jumpSeconds(e.shiftKey ? -5 : -1);
            }
            if (e.code === 'ArrowRight') {
                e.preventDefault();
                this.jumpSeconds(e.shiftKey ? 5 : 1);
            }
            if (e.code === 'Comma') {
                e.preventDefault();
                this.stepFrames(-1);
            }
            if (e.code === 'Period') {
                e.preventDefault();
                this.stepFrames(1);
            }
        });
    }

    isNativeKeyTarget(target) {
        if (!target || !target.tagName) return false;
        const tag = target.tagName.toUpperCase();
        return tag === 'INPUT'
            || tag === 'TEXTAREA'
            || tag === 'SELECT'
            || tag === 'BUTTON'
            || tag === 'VIDEO'
            || target.isContentEditable === true;
    }

    configureTranscriptSource() {
        if (this.hasTranscriptSource) return;

        if (this.searchBox) {
            this.searchBox.disabled = true;
            this.searchBox.hidden = true;
            this.searchBox.setAttribute('aria-disabled', 'true');
        }

        if (document.body?.dataset?.mode === 'analyze' && this.transcriptPanel) {
            this.transcriptPanel.classList.add('transcript-panel--disabled');
            this.transcriptPanel.setAttribute('aria-disabled', 'true');
        }
    }

    bindPrecisionControls() {
        if (this.playPauseBtn) {
            this.playPauseBtn.addEventListener('click', () => {
                this.video.paused ? this.safePlay() : this.video.pause();
            });
        }
        if (this.secondBackBtn) {
            this.secondBackBtn.addEventListener('click', () => this.jumpSeconds(-1));
        }
        if (this.secondForwardBtn) {
            this.secondForwardBtn.addEventListener('click', () => this.jumpSeconds(1));
        }
        if (this.stepBackBtn) {
            this.stepBackBtn.addEventListener('click', () => this.stepFrames(-1));
        }
        if (this.stepForwardBtn) {
            this.stepForwardBtn.addEventListener('click', () => this.stepFrames(1));
        }
        if (this.jumpBackBtn) {
            this.jumpBackBtn.addEventListener('click', () => this.jumpSeconds(-5));
        }
        if (this.jumpForwardBtn) {
            this.jumpForwardBtn.addEventListener('click', () => this.jumpSeconds(5));
        }
        if (this.captureFrameBtn) {
            this.captureFrameBtn.addEventListener('click', async () => {
                try {
                    const frame = await this.captureCurrentFrame();
                    document.dispatchEvent(
                        new CustomEvent('screenscribe:capture-frame', { detail: frame })
                    );
                } catch (error) {
                    if (DEBUG) console.error('Frame capture failed:', error);
                }
            });
        }
        if (this.frameSweep) {
            this.frameSweep.addEventListener('pointerdown', () => {
                this.isDraggingSweep = true;
            });
            this.frameSweep.addEventListener('pointerup', () => {
                this.isDraggingSweep = false;
            });
            this.frameSweep.addEventListener('input', (e) => {
                const target = e.target;
                const nextTime = Number(target.value);
                this.setCurrentTimeSafe(nextTime);
                this.updateControlState();
            });
            this.frameSweep.addEventListener('change', (e) => {
                const target = e.target;
                this.setCurrentTimeSafe(Number(target.value));
                this.updateControlState();
            });
        }
    }

    onMetadataLoaded() {
        const duration = Number(this.video.duration) || 0;
        if (this.frameSweep) {
            this.frameSweep.max = String(duration);
            this.frameSweep.step = String(this.frameStepSeconds);
        }
        this.updateControlState();
    }

    onTimeUpdate() {
        const currentTime = this.video.currentTime;
        let activeSegment = null;

        for (const segment of this.segments) {
            if (currentTime >= segment.start && currentTime < segment.end) {
                activeSegment = segment;
                break;
            }
        }

        if (activeSegment && activeSegment.id !== this.currentSegmentId) {
            this.currentSegmentId = activeSegment.id;
            this.updateActiveHighlight(activeSegment.id);
            this.updateSubtitleDisplay(activeSegment.text);
        } else if (!activeSegment && this.currentSegmentId !== null) {
            this.currentSegmentId = null;
            this.clearActiveHighlight();
            this.updateSubtitleDisplay(null);
        }

        this.updateControlState();
    }

    updateSubtitleDisplay(text) {
        if (!this.subtitleDisplay) return;

        if (text) {
            this.subtitleDisplay.textContent = text;
            this.subtitleDisplay.classList.remove('empty');
        } else {
            this.subtitleDisplay.textContent = t('media.noSubtitle');
            this.subtitleDisplay.classList.add('empty');
        }
    }

    updateActiveHighlight(segmentId) {
        document.querySelectorAll('.subtitle-item').forEach(item => {
            item.classList.remove('active');
        });

        const activeItem = document.querySelector(`[data-segment-id="${segmentId}"]`);
        if (activeItem) {
            activeItem.classList.add('active');
            activeItem.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
    }

    clearActiveHighlight() {
        document.querySelectorAll('.subtitle-item').forEach(item => {
            item.classList.remove('active');
        });
    }

    renderSubtitleList(segments) {
        if (!this.subtitleList) return;
        if (!this.hasTranscriptSource) return;
        this.subtitleList.replaceChildren();

        segments.forEach((segment) => {
            const item = document.createElement('div');
            item.className = 'subtitle-item';
            item.dataset.segmentId = String(segment.id);
            item.addEventListener('click', () => this.seekTo(segment.start, false));

            const timestamp = document.createElement('div');
            timestamp.className = 'timestamp';
            timestamp.textContent =
                `${this.formatTime(segment.start)} - ${this.formatTime(segment.end)} (${this.formatTimePrecise(segment.start)})`;

            const text = document.createElement('div');
            text.className = 'text';
            text.textContent = segment.text;

            item.appendChild(timestamp);
            item.appendChild(text);
            this.subtitleList.appendChild(item);
        });
    }

    seekTo(time, autoplay = true) {
        if (!this.video) return;
        this.setCurrentTimeSafe(time);
        this.updateControlState();
        if (autoplay) this.safePlay();
    }

    safePlay() {
        const playPromise = this.video.play();
        if (playPromise && typeof playPromise.catch === 'function') {
            playPromise.catch((error) => {
                if (error?.name === 'AbortError') return;
                console.warn('Video playback failed:', error);
                this.showVideoPlaybackError(error);
            });
        }
    }

    showVideoPlaybackError(error = null) {
        if (!this.subtitleDisplay) return;
        const unsupported = error?.name === 'NotSupportedError';
        this.subtitleDisplay.textContent = unsupported
            ? t('media.videoUnsupported')
            : t('media.videoPlaybackFailed');
        this.subtitleDisplay.classList.remove('empty');
    }

    jumpSeconds(deltaSeconds) {
        if (!this.video) return;
        this.setCurrentTimeSafe(this.video.currentTime + deltaSeconds);
        this.updateControlState();
    }

    stepFrames(direction) {
        if (!this.video) return;
        this.video.pause();
        this.setCurrentTimeSafe(this.video.currentTime + (this.frameStepSeconds * direction));
        this.updateControlState();
    }

    async captureCurrentFrame() {
        if (!this.video || !this.video.videoWidth || !this.video.videoHeight) {
            throw new Error('Video frame is not ready yet.');
        }
        const canvas = document.createElement('canvas');
        canvas.width = this.video.videoWidth;
        canvas.height = this.video.videoHeight;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(this.video, 0, 0);
        const dataUrl = canvas.toDataURL('image/jpeg', 0.92);
        return {
            timestamp: this.video.currentTime,
            frameBase64: dataUrl.split(',')[1],
            frameDataUrl: dataUrl,
        };
    }

    setCurrentTimeSafe(nextTime) {
        const duration = Number(this.video.duration) || 0;
        const bounded = Math.max(0, Math.min(duration || nextTime, nextTime));
        this.video.currentTime = bounded;
    }

    updatePlayPauseButton() {
        if (!this.playPauseBtn) return;
        this.playPauseBtn.textContent = this.video.paused
            ? t('media.playLabel')
            : t('media.pauseLabel');
    }

    updateControlState() {
        if (!this.video) return;
        const currentTime = Number(this.video.currentTime) || 0;
        const duration = Number(this.video.duration) || 0;
        if (this.frameSweep && !this.isDraggingSweep) {
            this.frameSweep.value = String(currentTime);
        }
        if (this.currentTimeLabel) {
            this.currentTimeLabel.textContent =
                `${this.formatTimePrecise(currentTime)} / ${this.formatTimePrecise(duration)}`;
        }
        this.updatePlayPauseButton();
    }

    formatTime(seconds) {
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = Math.floor(seconds % 60);
        return h > 0
            ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
            : `${m}:${String(s).padStart(2, '0')}`;
    }

    formatTimePrecise(seconds) {
        const safe = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
        const m = Math.floor(safe / 60);
        const s = Math.floor(safe % 60);
        const ms = Math.floor((safe % 1) * 1000);
        return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${String(ms).padStart(3, '0')}`;
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Global player instance
let player;
document.addEventListener('DOMContentLoaded', () => {
    player = new ScreenScribePlayer();
    window.player = player;
});
