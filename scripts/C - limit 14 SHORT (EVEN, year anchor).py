//@version=5
strategy("C - limit 14 SHORT (EVEN, year anchor)",
     overlay=true,
     pyramiding=300,
     initial_capital=100,
     default_qty_type=strategy.cash,
     commission_type=strategy.commission.percent,
     commission_value=0.0,
     process_orders_on_close=true,
     calc_on_order_fills=true)

// =====================
// Inputs
// =====================
firstSellUSDT     = input.float(5.0,  "First Short (USDT)", minval=0.1)
tpPercent         = input.float(1.1,  "TP % (Whole warehouse)", step=0.1)
callbackPercent   = input.float(0.2,  "Callback % (Trailing)", step=0.05)
marginCallLimit   = input.int(244,    "Margin Call Limit (max shorts)", minval=1)
linearRisePercent = input.float(0.5,  "Margin Call Rise % (after lvl 5)", step=0.1)

autoMerge         = input.bool(true, "Auto Merge")
subSellTPPercent  = input.float(1.3, "Sub-cover TP % (last lot)", step=0.1)

// Nonlinear initial rises (lvl2..lvl6)
rise1 = input.float(0.3, "Nonlinear rise lvl2 %", step=0.1)
rise2 = input.float(0.4, "Nonlinear rise lvl3 %", step=0.1)
rise3 = input.float(0.6, "Nonlinear rise lvl4 %", step=0.1)
rise4 = input.float(0.8, "Nonlinear rise lvl5 %", step=0.1)
rise5 = input.float(0.8, "Nonlinear rise lvl6 %", step=0.1)

// Multipliers (lvl2..lvl5), after lvl5 -> 1x
mult2 = input.float(1.5, "Multiplier lvl2", step=0.1)
mult3 = input.float(1.0, "Multiplier lvl3", step=0.1)
mult4 = input.float(2.0, "Multiplier lvl4", step=0.1)
mult5 = input.float(3.5, "Multiplier lvl5", step=0.1)

maxFillsPerBar    = input.int(6,  "Max DCA fills per bar", minval=1, maxval=20)
maxSubSellsPerBar = input.int(10, "Max SUB-covers per bar", minval=1, maxval=50)

// =====================
// Alert-safe throttle
// =====================
maxSignalsWindow = input.int(14, "Max signals per window", minval=1)
windowBars       = input.int(6,  "Window length (bars)",   minval=1)

// =====================
// Shared parity anchor
// =====================
anchorTz   = "UTC"
anchorTs   = timestamp(anchorTz, year, 1, 1, 0, 0)
barMs      = int(timeframe.in_seconds(timeframe.period) * 1000)
barsFromAnchor = int(math.floor((time - anchorTs) / barMs))
afterAnchor = time >= anchorTs

allowThisBar = afterAnchor and (barsFromAnchor % 2 == 0)

// =====================
// Rolling signal limiter
// =====================
var int signalsThisBar  = 0
var int signalsRolling  = 0

if barstate.isnew
    signalsRolling := nz(signalsRolling[1]) - nz(signalsThisBar[windowBars])
    if signalsRolling < 0
        signalsRolling := 0
    signalsThisBar := 0

f_canSignal() =>
    allowThisBar and (signalsRolling + signalsThisBar < maxSignalsWindow)

f_registerSignal() =>
    signalsThisBar = signalsThisBar + 1
    signalsRolling = signalsRolling + 1

// =====================
// Helper funcs
// =====================
f_getRiseForNextLevel(_numSells) =>
    ns = _numSells + 1
    ns == 2 ? rise1 :
     ns == 3 ? rise2 :
     ns == 4 ? rise3 :
     ns == 5 ? rise4 :
     ns == 6 ? rise5 :
     linearRisePercent

f_getMultForNextLevel(_numSells) =>
    ns = _numSells + 1
    ns == 2 ? mult2 :
     ns == 3 ? mult3 :
     ns == 4 ? mult4 :
     ns == 5 ? mult5 :
     1.0

f_nextLevel(_lastFillPrice, _numSells) =>
    r = f_getRiseForNextLevel(_numSells)
    _lastFillPrice * (1.0 + r/100.0)

f_recalcAvg(_proceeds, _sizeAbs) =>
    _sizeAbs > 0 ? _proceeds / _sizeAbs : na

// =====================
// State
// =====================
// posSizeAbs      — абсолютний розмір short-позиції у монетах
// posProceedsUSDT — сумарний USDT, отриманий від відкриття short-лотів
// avgPrice        — середня ціна відкриття short-складу
var float posSizeAbs     = 0.0
var float posProceedsUSDT= 0.0
var float avgPrice       = na
var int   numSells       = 0
var float lastFillPrice  = na
var float nextLevelPrice = na

// LIFO lots
var int lotCounter    = 0
var string[] lotIds   = array.new_string()
var float[]  lotQty   = array.new_float()
var float[]  lotPrice = array.new_float()

// trailing for full TP
var bool  trailingActive = false
var float trailingMin    = na

// cycle flags
var bool resetCycle = false
var bool restartedThisBar = false
restartedThisBar := false

// =====================
// RESET + immediate restart
// =====================
if resetCycle
    posSizeAbs := 0
    posProceedsUSDT := 0
    avgPrice := na
    numSells := 0
    lastFillPrice := na
    nextLevelPrice := na
    trailingActive := false
    trailingMin := na
    array.clear(lotIds), array.clear(lotQty), array.clear(lotPrice)
    resetCycle := false

    if f_canSignal()
        sellUSDT = firstSellUSDT
        lotCounter += 1
        id0 = "S" + str.tostring(lotCounter)

        strategy.entry(id0, strategy.short, qty=sellUSDT, comment="Restart Short_0")
        f_registerSignal()

        qty0 = sellUSDT / close
        fill0 = close

        array.push(lotIds, id0)
        array.push(lotQty, qty0)
        array.push(lotPrice, fill0)

        posSizeAbs      += qty0
        posProceedsUSDT += sellUSDT
        avgPrice        := fill0
        numSells        := 1
        lastFillPrice   := fill0
        nextLevelPrice  := f_nextLevel(lastFillPrice, numSells)

        restartedThisBar := true

// =====================
// Start new cycle if flat
// =====================
if posSizeAbs == 0 and array.size(lotIds) == 0 and not resetCycle and not restartedThisBar
    if f_canSignal()
        sellUSDT = firstSellUSDT
        lotCounter += 1
        id0 = "S" + str.tostring(lotCounter)

        strategy.entry(id0, strategy.short, qty=sellUSDT, comment="First Short_0")
        f_registerSignal()

        qty0 = sellUSDT / close
        fill0 = close

        array.push(lotIds, id0)
        array.push(lotQty, qty0)
        array.push(lotPrice, fill0)

        posSizeAbs      += qty0
        posProceedsUSDT += sellUSDT
        avgPrice        := fill0
        numSells        := 1
        lastFillPrice   := fill0
        nextLevelPrice  := f_nextLevel(lastFillPrice, numSells)

// =====================
// FULL TP (priority)
// Для short TP спрацьовує, коли ціна <= avgPrice * (1 - tp%)
// =====================
tpPrice = avgPrice * (1.0 - tpPercent/100.0)
tpHit   = not na(avgPrice) and close <= tpPrice

if tpHit
    if callbackPercent > 0
        trailingActive := true
        trailingMin := na(trailingMin) ? close : math.min(trailingMin, close)
        trailStop = trailingMin * (1.0 + callbackPercent/100.0)

        if close >= trailStop and f_canSignal()
            strategy.close_all(comment="TP Full (Trailing)")
            f_registerSignal()
            label.new(bar_index, close, "FULL COVER",
                 style=label.style_label_up,
                 color=color.yellow, textcolor=color.black, size=size.small)
            resetCycle := true
    else
        if f_canSignal()
            strategy.close_all(comment="TP Full")
            f_registerSignal()
            label.new(bar_index, close, "FULL COVER",
                 style=label.style_label_up,
                 color=color.yellow, textcolor=color.black, size=size.small)
            resetCycle := true

// =====================
// DCA shorts (TRUE LEVEL FILLS)
// Для short доливка відбувається ВГОРУ: high >= nextLevelPrice
// =====================
if not tpHit and posSizeAbs > 0 and not restartedThisBar
    canSellMore = numSells < marginCallLimit
    fills = 0

    while canSellMore and fills < maxFillsPerBar and not na(nextLevelPrice) and high >= nextLevelPrice and f_canSignal()
        mult = f_getMultForNextLevel(numSells)
        sellUSDT = firstSellUSDT * mult

        lotCounter += 1
        id = "S" + str.tostring(lotCounter)

        strategy.entry(id, strategy.short, qty=sellUSDT, limit=nextLevelPrice, comment="DCA Short")
        f_registerSignal()

        fillP = close // або nextLevelPrice, якщо хочеш жорстку симуляцію рівня
        qtyS  = sellUSDT / fillP

        array.push(lotIds, id)
        array.push(lotQty, qtyS)
        array.push(lotPrice, fillP)

        posSizeAbs      += qtyS
        posProceedsUSDT += sellUSDT
        avgPrice        := f_recalcAvg(posProceedsUSDT, posSizeAbs)

        numSells       += 1
        lastFillPrice  := fillP
        nextLevelPrice := f_nextLevel(lastFillPrice, numSells)

        trailingActive := false
        trailingMin := na

        fills += 1
        canSellMore := numSells < marginCallLimit

// =====================
// SUB-COVER (only after >5 sells, LIFO, cascade)
// Для short “sub-sell” дзеркалиться як частковий викуп останнього лота у плюс
// =====================
if not tpHit and posSizeAbs > 0 and numSells > 5 and not restartedThisBar
    covered = 0
    anyCovered = false

    while covered < maxSubSellsPerBar and f_canSignal()
        lastIdx = array.size(lotIds) - 1
        if lastIdx < 0
            break

        idLast    = array.get(lotIds, lastIdx)
        qtyLast   = array.get(lotQty, lastIdx)
        entryLast = array.get(lotPrice, lastIdx)

        lastLotTP = entryLast * (1.0 - subSellTPPercent/100.0)

        if close <= lastLotTP
            anyCovered := true
            strategy.close(idLast, comment="Sub-cover last lot")
            f_registerSignal()

            entryProceedsUSDT = qtyLast * entryLast
            profitUSDT        = qtyLast * (entryLast - close)

            posSizeAbs      -= qtyLast
            posProceedsUSDT -= entryProceedsUSDT

            if autoMerge
                posProceedsUSDT -= profitUSDT

            avgPrice := f_recalcAvg(posProceedsUSDT, posSizeAbs)

            array.pop(lotIds), array.pop(lotQty), array.pop(lotPrice)

            numSells := math.max(numSells - 1, 0)

            covered += 1

            if array.size(lotIds) == 0 or posSizeAbs <= 0
                resetCycle := true
                break
        else
            break

    if anyCovered and not resetCycle and array.size(lotPrice) > 0
        lastFillPrice := array.get(lotPrice, array.size(lotPrice) - 1)
        nextLevelPrice := f_nextLevel(lastFillPrice, numSells)

// =====================
// Plots
// =====================
plot(avgPrice, "Avg Price", color=color.new(color.yellow, 0), linewidth=2)
plot(tpPrice,  "TP Price",  color=color.new(color.green, 0), linewidth=2)
plot(nextLevelPrice, "Next DCA Level", color=color.new(color.red, 0), style=plot.style_cross)
plotchar(numSells, "Shorts Count", "", location=location.top)