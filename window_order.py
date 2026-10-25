#auftrag in das gewählte fenster tippen und abschicken
import comtypes
import comtypes.client
import ctypes
import threading
import time
from ctypes import wintypes

comtypes.client.GetModule("UIAutomationCore.dll")
from comtypes.gen import UIAutomationClient as UIA

ORDER_LOCK = threading.Lock()
MODE_LABEL = {"agent": "Agent", "plan": "Plan", "ask": "Ask"}
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
SW_RESTORE = 9
GA_ROOT = 2
VK_MENU = 0x12
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_RETURN = 0x0D
VK_BACK = 0x08
VK_A = 0x41
VK_ESCAPE = 0x1B
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = (("ki", KEYBDINPUT), ("mi", MOUSEINPUT))

    _anonymous_ = ("u",)
    _fields_ = (("type", wintypes.DWORD), ("u", _U))


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


class FensterFehler(Exception):
    pass


def hwnd_wert(hwnd):
    if isinstance(hwnd, int):
        return hwnd
    value = getattr(hwnd, "value", None)
    if isinstance(value, int):
        return value
    return int(hwnd)


def lauf_id(element):
    raw = element.GetRuntimeId()
    if raw is None:
        return ()
    try:
        return tuple(int(part) for part in raw)
    except TypeError:
        return (int(raw),)


def kasten(element):
    rect = element.CurrentBoundingRectangle
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def taste(vk, scan=0, flags=0):
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0))


def sende_tasten(folgen):
    if not folgen:
        return
    arr = (INPUT * len(folgen))(*folgen)
    sent = user32.SendInput(len(folgen), arr, ctypes.sizeof(INPUT))
    if sent != len(folgen):
        raise FensterFehler("tasten nicht angenommen")


def kombi(modifier, vk):
    sende_tasten([
        taste(modifier),
        taste(vk),
        taste(vk, flags=KEYEVENTF_KEYUP),
        taste(modifier, flags=KEYEVENTF_KEYUP),
    ])


def unicode_folgen(text):
    folgen = []
    roh = text.encode("utf-16-le")
    index = 0
    while index < len(roh):
        unit = roh[index] + (roh[index + 1] << 8)
        index += 2
        if unit in (10, 13):
            if unit == 13:
                continue
            folgen.append(taste(VK_SHIFT))
            folgen.append(taste(VK_RETURN))
            folgen.append(taste(VK_RETURN, flags=KEYEVENTF_KEYUP))
            folgen.append(taste(VK_SHIFT, flags=KEYEVENTF_KEYUP))
            continue
        folgen.append(taste(0, scan=unit, flags=KEYEVENTF_UNICODE))
        folgen.append(taste(0, scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return folgen


def tippe_text(text):
    #kleine portionen, sonst verschluckt das fenster buchstaben
    folgen = unicode_folgen(text)
    schritt = 16
    for start in range(0, len(folgen), schritt):
        sende_tasten(folgen[start:start + schritt])
        time.sleep(0.012)


def lese_clipboard():
    if not user32.OpenClipboard(None):
        return None
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def setze_clipboard(text):
    roh = (text + "\0").encode("utf-16-le")
    if not user32.OpenClipboard(None):
        raise FensterFehler("zwischenablage zu")
    try:
        if not user32.EmptyClipboard():
            raise FensterFehler("zwischenablage leer")
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(roh))
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise FensterFehler("zwischenablage speicher")
        ctypes.memmove(pointer, roh, len(roh))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise FensterFehler("zwischenablage setzen")
    finally:
        user32.CloseClipboard()


def fuege_ein():
    kombi(VK_CONTROL, 0x56)


def finde_render(hwnd):
    kinder = []

    def each(child, _lparam):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, buf, 256)
        if buf.value == "Chrome_RenderWidgetHostHWND":
            kinder.append(hwnd_wert(child))
        return True

    user32.EnumChildWindows(wintypes.HWND(hwnd), WNDENUMPROC(each), 0)
    if kinder:
        return kinder[0]
    return int(hwnd)


def neues_uia():
    return comtypes.CoCreateInstance(
        UIA.CUIAutomation._reg_clsid_,
        interface=UIA.IUIAutomation,
        clsctx=comtypes.CLSCTX_INPROC_SERVER,
    )


def alle_elemente(uia, root):
    gefunden = root.FindAll(UIA.TreeScope_Descendants, uia.CreateTrueCondition())
    menge = []
    for index in range(int(gefunden.Length)):
        menge.append(gefunden.GetElement(index))
    return menge


def lese_feld(element):
    #nur der inhalt des eingabefelds, nicht der ganze chat
    try:
        muster = element.GetCurrentPattern(int(UIA.UIA_ValuePatternId))
        wert = muster.QueryInterface(UIA.IUIAutomationValuePattern)
        return wert.CurrentValue or ""
    except Exception:
        pass
    try:
        roh = lese_text(element)
    except Exception:
        return None
    if roh is None or len(roh) > 500:
        return None
    return roh


def feld_gleich(element, text):
    ist = lese_feld(element)
    if ist is None:
        return None
    links = " ".join(ist.split()).casefold()
    rechts = " ".join((text or "").split()).casefold()
    if not rechts:
        return False
    if len(links) > len(rechts) + 80:
        return None
    return links == rechts


def lese_text(element):
    try:
        muster = element.GetCurrentPattern(UIA.UIA_TextPatternId)
        text = muster.QueryInterface(UIA.IUIAutomationTextPattern)
        return text.DocumentRange.GetText(4000) or ""
    except Exception:
        try:
            return element.CurrentName or ""
        except Exception:
            return ""


def ahne_hat(uia, element, wort):
    walker = uia.ControlViewWalker
    aktuell = element
    for _ in range(8):
        aktuell = walker.GetParentElement(aktuell)
        if not aktuell:
            return False
        name = (aktuell.CurrentName or "").casefold()
        if wort in name:
            return True
    return False


def bewerte_feld(uia, edit):
    name = (edit.CurrentName or "").casefold()
    if "find" in name or "suche" in name:
        return 0
    left, top, right, bottom = kasten(edit)
    hoehe = bottom - top
    breite = right - left
    punkte = 0
    if name in ("prompt", "chat", "message"):
        punkte += 100
    elif "prompt" in name or "follow" in name or "nachricht" in name:
        punkte += 60
    if 12 <= hoehe <= 180 and breite >= 80:
        punkte += 30
    if ahne_hat(uia, edit, "chat"):
        punkte += 40
    if punkte:
        punkte += max(0, top) // 200
    return punkte


def finde_feld(uia, root):
    beste = None
    beste_punkte = 0
    for element in alle_elemente(uia, root):
        if int(element.CurrentControlType) != int(UIA.UIA_EditControlTypeId):
            continue
        punkte = bewerte_feld(uia, element)
        if punkte > beste_punkte:
            beste = element
            beste_punkte = punkte
    if not beste or beste_punkte < 30:
        return None
    return beste


def fokus_fenster(hwnd):
    handle = wintypes.HWND(hwnd)
    if user32.IsIconic(handle):
        user32.ShowWindow(handle, SW_RESTORE)
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.SetForegroundWindow(handle)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.2)
    vorne = hwnd_wert(user32.GetForegroundWindow())
    wurzel = hwnd_wert(user32.GetAncestor(wintypes.HWND(vorne), GA_ROOT))
    return vorne == int(hwnd) or wurzel == int(hwnd)


def fokus_feld(edit):
    edit.SetFocus()
    time.sleep(0.15)
    return True


def fokus_stimmt(uia, edit, hwnd):
    if not fokus_fenster(hwnd):
        return False
    fokus_feld(edit)
    aktiv = uia.GetFocusedElement()
    if not aktiv:
        return False
    return lauf_id(aktiv) == lauf_id(edit)


def invoke(element):
    try:
        muster = element.GetCurrentPattern(UIA.UIA_InvokePatternId)
        muster.QueryInterface(UIA.IUIAutomationInvokePattern).Invoke()
        return True
    except Exception:
        return False


def modus_pill(root_elemente, edit):
    _left, _top, _right, bottom = kasten(edit)
    pill = None
    for element in root_elemente:
        if int(element.CurrentControlType) != int(UIA.UIA_TextControlTypeId):
            continue
        if (element.CurrentName or "") not in MODE_LABEL.values():
            continue
        left, top, right, unten = kasten(element)
        if unten <= bottom - 2 or top > bottom + 50:
            continue
        if right - left < 16:
            continue
        pill = element
    return pill


def setze_modus(uia, root, edit, mode):
    label = MODE_LABEL.get(mode)
    if not label:
        return False
    elemente = alle_elemente(uia, root)
    pill = modus_pill(elemente, edit)
    if not pill:
        return False
    if (pill.CurrentName or "") == label:
        return True
    eltern = uia.ControlViewWalker.GetParentElement(pill)
    if not eltern or not invoke(eltern):
        return False
    time.sleep(0.25)
    ziel_id = lauf_id(pill)
    _left, top, _right, _bottom = kasten(pill)
    treffer = None
    for element in alle_elemente(uia, root):
        if int(element.CurrentControlType) != int(UIA.UIA_TextControlTypeId):
            continue
        if (element.CurrentName or "") != label:
            continue
        if lauf_id(element) == ziel_id:
            continue
        if kasten(element)[1] > top + 8:
            continue
        treffer = element
        break
    if not treffer:
        sende_tasten([taste(VK_ESCAPE), taste(VK_ESCAPE, flags=KEYEVENTF_KEYUP)])
        return False
    eltern = uia.ControlViewWalker.GetParentElement(treffer)
    if not eltern or not invoke(eltern):
        sende_tasten([taste(VK_ESCAPE), taste(VK_ESCAPE, flags=KEYEVENTF_KEYUP)])
        return False
    time.sleep(0.2)
    return True


def text_sichtbar(uia, root, text):
    nadel = " ".join(text.split())
    if len(nadel) > 80:
        nadel = nadel[:80]
    nadel = nadel.casefold()
    if not nadel:
        return False
    try:
        elemente = alle_elemente(uia, root)
    except Exception:
        return False
    for element in elemente:
        try:
            name = (element.CurrentName or "").casefold()
        except Exception:
            continue
        if nadel in " ".join(name.split()):
            return True
    return False


def leere_feld():
    kombi(VK_CONTROL, VK_A)
    sende_tasten([taste(VK_BACK), taste(VK_BACK, flags=KEYEVENTF_KEYUP)])


def fuege_text_ein(text):
    alt = lese_clipboard()
    try:
        setze_clipboard(text)
        fuege_ein()
        time.sleep(0.2)
        return True
    except FensterFehler:
        return False
    finally:
        if alt is not None:
            try:
                setze_clipboard(alt)
            except FensterFehler:
                pass


def schreibe_ins_feld(edit, text):
    #erst einfügen, tippen nur wenn das feld den text nicht hat
    if feld_gleich(edit, text) is True:
        return
    if lese_feld(edit):
        leere_feld()
        time.sleep(0.08)
    if not fuege_text_ein(text):
        tippe_text(text)
        time.sleep(0.12)
    if feld_gleich(edit, text) is False:
        leere_feld()
        time.sleep(0.08)
        tippe_text(text)
        time.sleep(0.12)
    if feld_gleich(edit, text) is False:
        raise FensterFehler("text nicht im feld")


def enter_senden():
    sende_tasten([taste(VK_RETURN), taste(VK_RETURN, flags=KEYEVENTF_KEYUP)])


def sende_auftrag(hwnd, text, mode):
    #nur dieses fenster, keine andere app
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise FensterFehler("text fehlt")
    if len(text) > 8000:
        raise FensterFehler("text zu lang")
    if not ORDER_LOCK.acquire(timeout=2):
        raise FensterFehler("fenster arbeitet noch")
    comtypes.CoInitialize()
    try:
        handle = int(hwnd)
        if not user32.IsWindow(wintypes.HWND(handle)):
            raise FensterFehler("fenster weg")
        render = finde_render(handle)
        uia = neues_uia()
        root = uia.ElementFromHandle(render)
        feld = finde_feld(uia, root)
        if not feld:
            time.sleep(0.4)
            feld = finde_feld(uia, root)
        if not feld:
            raise FensterFehler("kein eingabefeld")
        if not fokus_stimmt(uia, feld, handle):
            raise FensterFehler("fokus nicht im feld")
        modus_ok = False
        try:
            modus_ok = setze_modus(uia, root, feld, mode)
        except Exception:
            modus_ok = False
        if not fokus_stimmt(uia, feld, handle):
            raise FensterFehler("fokus nicht im feld")
        schreibe_ins_feld(feld, text)
        enter_senden()
        time.sleep(0.3)
        #zweites enter nur wenn der text noch im feld steht
        if feld_gleich(feld, text) is True:
            kombi(VK_CONTROL, VK_RETURN)
            time.sleep(0.3)
        if feld_gleich(feld, text) is True:
            raise FensterFehler("nicht abgeschickt")
        print("auftrag im fenster %s modus %s ok=%s" % (handle, mode, modus_ok), flush=True)
        return modus_ok
    finally:
        ORDER_LOCK.release()
        comtypes.CoUninitialize()


def stoppe_auftrag(hwnd):
    if not ORDER_LOCK.acquire(timeout=1):
        return False
    comtypes.CoInitialize()
    try:
        handle = int(hwnd)
        if not user32.IsWindow(wintypes.HWND(handle)):
            return False
        uia = neues_uia()
        root = uia.ElementFromHandle(finde_render(handle))
        for element in alle_elemente(uia, root):
            if int(element.CurrentControlType) != int(UIA.UIA_ButtonControlTypeId):
                continue
            name = element.CurrentName or ""
            if name.lower().startswith("stop"):
                return invoke(element)
        return False
    finally:
        ORDER_LOCK.release()
        comtypes.CoUninitialize()
