sub init()
    ' URL da imagem vem da injecao via ECP (contentId) ou do manifest (snapshot_url).
    ' Ex.: GET http://192.168.0.100:8060/launch/880042?contentId=http://servidor:8080/api/snapshot.png
    m.imageUrl = m.top.contentId
    if m.imageUrl = invalid or m.imageUrl = "" then
        m.appInfo = CreateObject("roAppInfo")
        m.imageUrl = m.appInfo.GetValue("snapshot_url")
    end if
    if m.imageUrl = invalid then m.imageUrl = ""
    
    m.poster1 = m.top.findNode("poster1")
    m.poster2 = m.top.findNode("poster2")
    m.timer = m.top.findNode("refreshTimer")
    
    ' Observar quando a imagem termina de carregar no poster oculto
    m.poster1.observeField("loadStatus", "onLoadStatusChange")
    m.poster2.observeField("loadStatus", "onLoadStatusChange")
    
    m.timer.observeField("fire", "onTimer")
    m.timer.control = "start"
    
    m.counter = 0
    m.activePoster = 1
    
    carregarImagem()
end sub

sub onTimer()
    carregarImagem()
end sub

sub carregarImagem()
    if m.imageUrl = "" then return
    
    m.counter = m.counter + 1
    nextUrl = m.imageUrl + "?c=" + m.counter.toStr()
    
    ' Carrega a imagem no poster que está OCULTO
    if m.activePoster = 1 then
        m.poster2.uri = nextUrl
    else
        m.poster1.uri = nextUrl
    end if
end sub

sub onLoadStatusChange(event as Object)
    poster = event.getRoSGNode()
    status = event.getData()
    
    ' Só troca a visibilidade quando a imagem terminar de carregar (ready)
    if status = "ready" then
        if poster.id = "poster2" and m.activePoster = 1 then
            m.poster2.visible = true
            m.poster1.visible = false
            m.activePoster = 2
        else if poster.id = "poster1" and m.activePoster = 2 then
            m.poster1.visible = true
            m.poster2.visible = false
            m.activePoster = 1
        end if
    end if
end sub
