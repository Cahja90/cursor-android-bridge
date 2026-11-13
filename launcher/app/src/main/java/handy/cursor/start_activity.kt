package handy.cursor

import android.app.Activity
import android.net.http.SslError
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.WindowManager
import android.webkit.CookieManager
import android.webkit.SslErrorHandler
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout

class StartActivity : Activity() {
    private var web_view: WebView? = null
    private var url_index = 0
    private var seite_fertig = false
    private val wechsel = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        val root = FrameLayout(this)
        root.setBackgroundColor(0xFF181818.toInt())
        val web = WebView(this)
        web.setBackgroundColor(0xFF181818.toInt())
        root.addView(
            web,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT,
            ),
        )
        setContentView(root)
        web_view = web
        prepare_web(web)
        oeffne_aktuelle(web)
    }

    private fun seiten_liste(): List<String> {
        val liste = ArrayList<String>()
        liste.add(getString(R.string.page_url))
        liste.add(getString(R.string.page_url_http))
        val weg = getString(R.string.page_url_away)
        if (weg.startsWith("http")) {
            liste.add(weg)
        }
        return liste
    }

    private fun oeffne_aktuelle(view: WebView) {
        val liste = seiten_liste()
        if (url_index >= liste.size) {
            return
        }
        seite_fertig = false
        view.loadUrl(liste[url_index])
        wechsel.removeCallbacksAndMessages(null)
        wechsel.postDelayed({
            if (seite_fertig) {
                return@postDelayed
            }
            oeffne_naechste(view)
        }, 5000)
    }

    private fun oeffne_naechste(view: WebView) {
        wechsel.removeCallbacksAndMessages(null)
        val liste = seiten_liste()
        url_index += 1
        if (url_index >= liste.size) {
            return
        }
        oeffne_aktuelle(view)
    }

    private fun prepare_web(web: WebView) {
        val settings = web.settings
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.useWideViewPort = true
        settings.loadWithOverviewMode = true
        settings.mediaPlaybackRequiresUserGesture = false
        val cookies = CookieManager.getInstance()
        cookies.setAcceptCookie(true)
        cookies.setAcceptThirdPartyCookies(web, true)
        web.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView, url: String) {
                seite_fertig = true
                wechsel.removeCallbacksAndMessages(null)
            }

            override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
                //privates zertifikat bleibt im launcher, sonst naechste adresse
                handler.cancel()
                oeffne_naechste(view)
            }

            override fun onReceivedError(
                view: WebView,
                request: WebResourceRequest,
                error: WebResourceError,
            ) {
                if (request.isForMainFrame) oeffne_naechste(view)
            }
        }
    }

    override fun onBackPressed() {
        val web = web_view
        if (web != null && web.canGoBack()) {
            web.goBack()
            return
        }
        super.onBackPressed()
    }
}
