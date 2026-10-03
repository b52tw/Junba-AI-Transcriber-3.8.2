package tw.junba.transcriber

import android.content.Context
import dev.ffmpegkit.whisper.Whisper
import dev.ffmpegkit.whisper.WhisperConfig
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

interface WhisperCallback {
    fun onSuccess(text: String)
    fun onError(message: String)
}

object LocalWhisperEngine {
    private val scope = CoroutineScope(Dispatchers.Main)

    @JvmStatic
    fun transcribeAsync(
        context: Context,
        modelPath: String,
        audioPath: String,
        language: String,
        callback: WhisperCallback
    ) {
        scope.launch {
            try {
                val output = withContext(Dispatchers.IO) {
                    val wav = AudioPreprocessor.toWhisperWav(context, audioPath)
                    val model = Whisper.loadModel(context, modelPath)
                    try {
                        val threads = Runtime.getRuntime().availableProcessors().coerceIn(2, 6)
                        val cfg = if (language.isBlank()) WhisperConfig(threads = threads) else WhisperConfig(language = language, threads = threads)
                        val r = Whisper.transcribe(model, wav.absolutePath, cfg)
                        if (r.segments.isNotEmpty()) {
                            buildString {
                                r.segments.forEach { s ->
                                    append('[')
                                    append(formatMs(s.startMs))
                                    append('–')
                                    append(formatMs(s.endMs))
                                    append("] ")
                                    append(s.text.trim())
                                    append('\n')
                                }
                            }.trim()
                        } else {
                            r.text.trim()
                        }
                    } finally {
                        Whisper.releaseModel(model)
                        wav.delete()
                    }
                }
                callback.onSuccess(output)
            } catch (t: Throwable) {
                callback.onError("${t::class.java.simpleName}: ${t.message ?: "本機 Whisper 失敗"}")
            }
        }
    }

    @JvmStatic
    fun systemInfo(): String = try { Whisper.getSystemInfo() } catch (_: Throwable) { "Whisper.cpp" }

    private fun formatMs(ms: Long): String {
        val total = ms.coerceAtLeast(0L) / 1000L
        val h = total / 3600L
        val m = (total % 3600L) / 60L
        val s = total % 60L
        return if (h > 0) "%02d:%02d:%02d".format(h, m, s) else "%02d:%02d".format(m, s)
    }
}
