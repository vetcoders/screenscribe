(function attachSttTransport(root) {
    const namespace = root.ScreenScribeLib || {};
    const DEFAULT_AUDIO_CONSTRAINTS = {
        echoCancellation: true,
        noiseSuppression: true,
        sampleRate: 16000,
    };

    async function extractErrorDetail(response, fallbackMessage) {
        let detail = fallbackMessage || `STT failed: ${response.status}`;
        try {
            const payload = await response.json();
            if (payload && typeof payload.detail === 'string' && payload.detail.trim()) {
                detail = payload.detail.trim();
            }
        } catch (_error) {
            // Keep HTTP status fallback if server returned no JSON detail.
        }
        if ((response.status === 401 || response.status === 403) && !String(detail).includes('HTTP')) {
            detail = `${detail} (HTTP ${response.status})`;
        }
        return detail;
    }

    async function postStt(audioBlob, options) {
        const formData = new FormData();
        formData.append('audio', audioBlob, options?.filename || 'recording.webm');

        options?.onTranscribingChange?.(true);
        options?.onStatus?.(options?.statusTranscribing, 'busy');
        try {
            const response = await fetch('/api/stt', {
                method: 'POST',
                body: formData,
            });

            if (!response.ok) {
                throw new Error(await extractErrorDetail(response, options?.fallbackMessage));
            }

            const result = await response.json();
            options?.onTranscript?.(result.text || '');
            options?.onStatus?.(options?.statusReady, 'success');
            return result;
        } catch (error) {
            const message = error instanceof Error && error.message
                ? error.message
                : (options?.fallbackMessage || 'Transcription failed');
            options?.onError?.(error);
            options?.onStatus?.(message, 'error');
            return null;
        } finally {
            options?.onTranscribingChange?.(false);
        }
    }

    function createSttTransport(options) {
        const transport = {
            mediaRecorder: null,
            audioChunks: [],
            stream: null,
            isRecording: false,

            async init() {
                try {
                    this.stream = await navigator.mediaDevices.getUserMedia({
                        audio: options?.audioConstraints || DEFAULT_AUDIO_CONSTRAINTS,
                    });
                    return true;
                } catch (error) {
                    options?.onMicError?.(error);
                    return false;
                }
            },

            async start() {
                if (!this.stream) {
                    const ok = await this.init();
                    if (!ok) return false;
                }

                this.audioChunks = [];
                try {
                    this.mediaRecorder = new MediaRecorder(this.stream, {
                        mimeType: options?.mimeType || 'audio/webm;codecs=opus',
                    });
                } catch (error) {
                    this.releaseStreamTracks();
                    throw error;
                }

                this.mediaRecorder.ondataavailable = (event) => {
                    if (event.data.size > 0) {
                        this.audioChunks.push(event.data);
                    }
                };

                this.mediaRecorder.onstop = async () => {
                    const audioSize = this.audioChunks.reduce((total, chunk) => {
                        const size = Number(chunk?.size || 0);
                        return total + (Number.isFinite(size) ? size : 0);
                    }, 0);
                    if (options?.shouldTranscribe && !options.shouldTranscribe(audioSize, this.audioChunks)) {
                        options?.onDiscard?.(audioSize);
                        return;
                    }
                    const audioBlob = new Blob(this.audioChunks, { type: options?.blobType || 'audio/webm' });
                    await postStt(audioBlob, options);
                };

                try {
                    this.mediaRecorder.start();
                    this.isRecording = true;
                    options?.onRecordingStart?.();
                    return true;
                } catch (error) {
                    this.releaseStreamTracks();
                    throw error;
                }
            },

            stop() {
                if (this.mediaRecorder && this.isRecording) {
                    try {
                        this.mediaRecorder.stop();
                    } finally {
                        this.isRecording = false;
                        this.releaseStreamTracks();
                    }
                }
            },

            releaseStreamTracks() {
                if (this.stream) {
                    const tracks = typeof this.stream.getAudioTracks === 'function'
                        ? this.stream.getAudioTracks()
                        : this.stream.getTracks();
                    tracks.forEach((track) => {
                        if (typeof track.stop === 'function') {
                            track.stop();
                        }
                    });
                    this.stream = null;
                }
            },

            destroy() {
                if (this.mediaRecorder && this.isRecording) {
                    this.stop();
                    return;
                }
                this.releaseStreamTracks();
            },
        };
        return transport;
    }

    namespace.postStt = postStt;
    namespace.createSttTransport = createSttTransport;
    root.ScreenScribeLib = namespace;
})(typeof window !== 'undefined' ? window : globalThis);
