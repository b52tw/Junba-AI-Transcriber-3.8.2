package tw.junba.transcriber

import android.content.Context
import com.arthenica.ffmpegkit.FFmpegKit
import com.arthenica.ffmpegkit.ReturnCode
import java.io.File

object AudioPreprocessor {
    fun toWhisperWav(context: Context, inputPath: String): File {
        val input = File(inputPath)
        if (!input.isFile) error("找不到音檔：$inputPath")
        val out = File(context.cacheDir, "w_${System.nanoTime()}.wav")
        val cmd = "-hide_banner -loglevel error -y -i ${q(input.absolutePath)} -vn -ac 1 -ar 16000 -c:a pcm_s16le ${q(out.absolutePath)}"
        val session = FFmpegKit.execute(cmd)
        if (!ReturnCode.isSuccess(session.returnCode) || !out.isFile || out.length() < 44L) {
            out.delete()
            val log = session.allLogsAsString ?: ""
            error("音訊轉換失敗：${log.takeLast(500)}")
        }
        return out
    }

    private fun q(path: String): String = "\"${path.replace("\"", "\\\"")}\""
}
