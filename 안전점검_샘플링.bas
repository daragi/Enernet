Attribute VB_Name = "SamplingModule"
'-----------------------------------------------------------------
' Safety Inspection Sampling Macro
' - Run on RAW sheet only
' - Extract 12% randomly for ALL inspectors in the inspector column
' - Prevent duplicates using status column
' - Ask to reset previous status on run
' - Skip inspectors with no more available rows
' - Append _2, _3 ... if sheet name already exists
'-----------------------------------------------------------------

Sub SamplingInspector()

    Dim wsRaw As Worksheet
    Dim inspectorCol As Long
    Dim statusCol As Long
    Dim c As Long, lastCol As Long
    Dim dataStartRow As Long, lastRow As Long
    Dim r As Long, i As Long, j As Long, k As Long
    Dim tempVal As Long
    Dim inspectors As New Collection
    Dim val As String
    Dim allSelected As New Collection
    Dim inspector As Variant
    Dim totalCount As Long
    Dim poolCount As Long
    Dim sampleCount As Long
    Dim inspectorRows() As Long
    Dim rowIdx As Long
    Dim selectedCount As Long
    Dim finalSelectedRows() As Long
    Dim baseSheetName As String, newSheetName As String
    Dim suffix As String
    Dim sheetNum As Integer
    Dim wsNew As Worksheet
    Dim destRow As Long
    Dim hasY As Boolean
    Dim ans As Integer
    Dim prevExtractedRows As Long
    Dim skippedInspectors As Long

    '--- 1. Check RAW Sheet ---
    If ActiveSheet.Name <> GetKoStr("RawSheetName") Then
        MsgBox GetKoStr("MsgRawSheetOnly"), vbExclamation, GetKoStr("ErrExec")
        Exit Sub
    End If
    Set wsRaw = ActiveSheet

    '--- 2. Find Inspector Column Position ---
    inspectorCol = 0
    lastCol = wsRaw.Cells(1, wsRaw.Columns.Count).End(xlToLeft).Column
    For c = 1 To lastCol
        If Trim(wsRaw.Cells(1, c).Value) = GetKoStr("InspectorHeader") Then
            inspectorCol = c
            Exit For
        End If
    Next c
    If inspectorCol = 0 Then
        MsgBox GetKoStr("ErrColNotFound") & vbNewLine & _
               GetKoStr("ErrCheckHeader"), vbExclamation, GetKoStr("ErrCol")
        Exit Sub
    End If

    '--- 3. Set Data Range (From Row 2: Row 1=Headers) ---
    dataStartRow = 2
    lastRow = wsRaw.Cells(wsRaw.Rows.Count, "A").End(xlUp).Row
    If lastRow < dataStartRow Then
        MsgBox GetKoStr("ErrNoData"), vbExclamation, GetKoStr("ErrData")
        Exit Sub
    End If

    '--- 4. Find or Create Status Column ---
    statusCol = 0
    For c = 1 To lastCol
        If Trim(wsRaw.Cells(1, c).Value) = GetKoStr("StatusHeader") Then
            statusCol = c
            Exit For
        End If
    Next c
    
    If statusCol = 0 Then
        statusCol = lastCol + 1
        wsRaw.Cells(1, statusCol).Value = GetKoStr("StatusHeader")
        wsRaw.Cells(1, statusCol).Font.Bold = True
    End If

    '--- 5. Check for Previous Detections and Ask to Reset ---
    hasY = False
    For r = dataStartRow To lastRow
        If Trim(wsRaw.Cells(r, statusCol).Value) = "Y" Then
            hasY = True
            Exit For
        End If
    Next r

    If hasY Then
        ans = MsgBox(GetKoStr("MsgAskReset"), vbYesNoCancel + vbQuestion, GetKoStr("AskResetTitle"))
        If ans = vbYes Then
            For r = dataStartRow To lastRow
                wsRaw.Cells(r, statusCol).Value = ""
            Next r
        ElseIf ans = vbCancel Then
            Exit Sub
        End If
    End If

    '--- 6. Count Previous Extracted Rows ---
    prevExtractedRows = 0
    For r = dataStartRow To lastRow
        If Trim(wsRaw.Cells(r, statusCol).Value) = "Y" Then
            prevExtractedRows = prevExtractedRows + 1
        End If
    Next r

    '--- 7. Extract Unique Inspectors ---
    On Error Resume Next
    For r = dataStartRow To lastRow
        val = Trim(wsRaw.Cells(r, inspectorCol).Value)
        If val <> "" Then
            inspectors.Add val, val
        End If
    Next r
    On Error GoTo 0

    If inspectors.Count = 0 Then
        MsgBox GetKoStr("ErrNoData"), vbExclamation, GetKoStr("ErrData")
        Exit Sub
    End If

    '--- 8. Perform 12% Random Sampling for Each Inspector ---
    skippedInspectors = 0
    For Each inspector In inspectors
        ' Count total and available pool rows for this inspector
        totalCount = 0
        poolCount = 0
        For r = dataStartRow To lastRow
            If Trim(wsRaw.Cells(r, inspectorCol).Value) = inspector Then
                totalCount = totalCount + 1
                If Trim(wsRaw.Cells(r, statusCol).Value) <> "Y" Then
                    poolCount = poolCount + 1
                End If
            End If
        Next r

        If poolCount = 0 Then
            ' Skip this inspector as they have no more data to extract
            skippedInspectors = skippedInspectors + 1
        Else
            ' Build array of available pool row numbers
            ReDim inspectorRows(1 To poolCount)
            rowIdx = 0
            For r = dataStartRow To lastRow
                If Trim(wsRaw.Cells(r, inspectorCol).Value) = inspector Then
                    If Trim(wsRaw.Cells(r, statusCol).Value) <> "Y" Then
                        rowIdx = rowIdx + 1
                        inspectorRows(rowIdx) = r
                    End If
                End If
            Next r

            ' Calculate sample size (12% of totalCount, rounded, min 1)
            sampleCount = CLng(totalCount * 0.12)
            If sampleCount < 1 Then sampleCount = 1

            ' If remaining rows is less than sampleCount, extract all remaining
            If poolCount < sampleCount Then
                sampleCount = poolCount
            End If

            ' Fisher-Yates shuffle
            Randomize
            For i = 1 To sampleCount
                j = Int((poolCount - i + 1) * Rnd) + i
                tempVal = inspectorRows(i)
                inspectorRows(i) = inspectorRows(j)
                inspectorRows(j) = tempVal
            Next i

            ' Add selected rows to master collection
            For i = 1 To sampleCount
                allSelected.Add inspectorRows(i)
            Next i
        End If
    Next inspector

    '--- 9. Sort Selected Rows in Ascending Order ---
    selectedCount = allSelected.Count
    If selectedCount = 0 Then
        MsgBox GetKoStr("ErrNoData"), vbInformation, GetKoStr("Success")
        Exit Sub
    End If

    ReDim finalSelectedRows(1 To selectedCount)
    For i = 1 To selectedCount
        finalSelectedRows(i) = allSelected(i)
    Next i

    For i = 1 To selectedCount - 1
        For k = 1 To selectedCount - i
            If finalSelectedRows(k) > finalSelectedRows(k + 1) Then
                tempVal = finalSelectedRows(k)
                finalSelectedRows(k) = finalSelectedRows(k + 1)
                finalSelectedRows(k + 1) = tempVal
            End If
        Next k
    Next i

    '--- 10. Determine Sheet Name (Handle Duplicates) ---
    baseSheetName = GetKoStr("SheetAllPrefix")
    sheetNum = 1
    Do
        newSheetName = baseSheetName & sheetNum
        sheetNum = sheetNum + 1
    Loop While SheetExists(newSheetName)

    '--- 11. Create New Sheet ---
    Set wsNew = ActiveWorkbook.Sheets.Add(After:=ActiveWorkbook.Sheets(ActiveWorkbook.Sheets.Count))
    wsNew.Name = newSheetName

    '--- 12. Copy Header Rows (1) ---
    wsRaw.Rows(1).Copy wsNew.Rows(1)

    '--- 13. Copy Selected Rows & Mark status as 'Y' ---
    destRow = 2
    For i = 1 To selectedCount
        wsRaw.Cells(finalSelectedRows(i), statusCol).Value = "Y"
        wsRaw.Rows(finalSelectedRows(i)).Copy wsNew.Rows(destRow)
        destRow = destRow + 1
    Next i
    Application.CutCopyMode = False

    '--- 14. Remove Status Column & Auto-fit in New Sheet ---
    wsNew.Columns(statusCol).Delete
    wsNew.Columns.AutoFit
    wsNew.Rows(1).Font.Bold = True

    '--- 15. Show Completion Message ---
    MsgBox GetKoStr("MsgSuccessTitle") & vbNewLine & vbNewLine & _
           GetKoStr("MsgSuccessInspectors") & inspectors.Count & GetKoStr("MsgSuccessPersonUnit") & _
           " (" & GetKoStr("MsgSuccessSkipped") & skippedInspectors & GetKoStr("MsgSuccessPersonUnit") & ")" & vbNewLine & _
           GetKoStr("MsgSuccessTotalData") & (lastRow - dataStartRow + 1) & GetKoStr("MsgSuccessUnit") & vbNewLine & _
           GetKoStr("MsgSuccessPrevData") & prevExtractedRows & GetKoStr("MsgSuccessUnit") & vbNewLine & _
           GetKoStr("MsgSuccessSampleData") & selectedCount & GetKoStr("MsgSuccessUnit") & vbNewLine & vbNewLine & _
           GetKoStr("MsgSuccess7") & newSheetName & GetKoStr("MsgSuccess8"), vbInformation, GetKoStr("Success")

    wsNew.Activate

End Sub

'-----------------------------------------------------------------
' Helper function to check if sheet exists
'-----------------------------------------------------------------
Function SheetExists(sheetName As String) As Boolean
    Dim ws As Worksheet
    SheetExists = False
    For Each ws In ActiveWorkbook.Sheets
        If ws.Name = sheetName Then
            SheetExists = True
            Exit Function
        End If
    Next ws
End Function

'-----------------------------------------------------------------
' Helper function to retrieve Korean strings using ChrW (ASCII safe)
'-----------------------------------------------------------------
Function GetKoStr(key As String) As String
    Select Case key
        Case "RawSheetName"
            GetKoStr = "RAW"
        Case "MsgRawSheetOnly"
            GetKoStr = "RAW " & ChrW(49884) & ChrW(53944) & ChrW(50640) & ChrW(49436) & ChrW(47564) & " " & ChrW(49892) & ChrW(54665) & ChrW(54624) & " " & ChrW(49688) & " " & ChrW(51080) & ChrW(49845) & ChrW(45768) & ChrW(45796) & "."
        Case "ErrExec"
            GetKoStr = ChrW(49892) & ChrW(54665) & " " & ChrW(50724) & ChrW(47448)
        Case "InspectorHeader"
            GetKoStr = ChrW(51216) & ChrW(44160) & ChrW(50896)
        Case "ErrColNotFound"
            GetKoStr = ChrW(51216) & ChrW(44160) & ChrW(50896) & " " & ChrW(52972) & ChrW(47100) & " " & ChrW(51012) & " " & ChrW(52286) & " " & ChrW(51012) & " " & ChrW(49688) & " " & ChrW(50630) & ChrW(49845) & ChrW(45768) & ChrW(45796) & "."
        Case "ErrCheckHeader"
            GetKoStr = "1" & ChrW(54665) & ChrW(50640) & " " & ChrW(51216) & ChrW(44160) & ChrW(50896) & " " & ChrW(54756) & ChrW(45908) & ChrW(44032) & " " & ChrW(51080) & ChrW(45716) & ChrW(51648) & " " & ChrW(54869) & ChrW(51064) & ChrW(54644) & ChrW(51452) & ChrW(49464) & ChrW(50836) & "."
        Case "ErrCol"
            GetKoStr = ChrW(52972) & ChrW(47100) & " " & ChrW(50724) & ChrW(47448)
        Case "ErrNoData"
            GetKoStr = ChrW(45936) & ChrW(51060) & ChrW(53552) & ChrW(44032) & " " & ChrW(50630) & ChrW(49845) & ChrW(45768) & ChrW(45796) & "."
        Case "ErrData"
            GetKoStr = ChrW(45936) & ChrW(51060) & ChrW(53552) & " " & ChrW(50724) & ChrW(47448)
        Case "SheetAllPrefix"
            GetKoStr = ChrW(49368) & ChrW(54540) & ChrW(47553) & ChrW(45936) & ChrW(51060) & ChrW(53552)
        Case "MsgSuccessTitle"
            GetKoStr = ChrW(49368) & ChrW(54540) & ChrW(47553) & " " & ChrW(50756) & ChrW(47308) & "!"
        Case "MsgSuccessInspectors"
            GetKoStr = ChrW(45824) & ChrW(49345) & " " & ChrW(51216) & ChrW(44160) & ChrW(50896) & " " & ChrW(49688) & ": "
        Case "MsgSuccessPersonUnit"
            GetKoStr = ChrW(47749)
        Case "MsgSuccessSkipped"
            GetKoStr = ChrW(44148) & ChrW(45320) & ChrW(46848) & ": "
        Case "MsgSuccessTotalData"
            GetKoStr = ChrW(51204) & ChrW(52404) & " " & ChrW(45936) & ChrW(51060) & ChrW(53552) & "  : "
        Case "MsgSuccessUnit"
            GetKoStr = ChrW(44060)
        Case "MsgSuccessPrevData"
            GetKoStr = ChrW(44592) & ChrW(52628) & ChrW(52636) & " " & ChrW(45936) & ChrW(51060) & ChrW(53552) & ": "
        Case "MsgSuccessSampleData"
            GetKoStr = ChrW(51060) & ChrW(48264) & " " & ChrW(52628) & ChrW(52636) & " " & ChrW(49368) & ChrW(54540) & ": "
        Case "MsgSuccess7"
            GetKoStr = ChrW(49373) & ChrW(49457) & ChrW(46108) & " " & ChrW(49884) & ChrW(53944) & ": ["
        Case "MsgSuccess8"
            GetKoStr = "]"
        Case "Success"
            GetKoStr = ChrW(50756) & ChrW(47308)
        Case "StatusHeader"
            GetKoStr = ChrW(52628) & ChrW(52636) & ChrW(50668) & ChrW(48512)
        Case "MsgAskReset"
            GetKoStr = ChrW(51060) & ChrW(51204) & " " & ChrW(49368) & ChrW(54540) & ChrW(47553) & " " & ChrW(52628) & ChrW(52636) & " " & ChrW(44592) & ChrW(47197) & ChrW(51060) & " " & ChrW(51316) & ChrW(51116) & ChrW(54633) & ChrW(45768) & ChrW(45796) & ". " & ChrW(44592) & ChrW(51316) & " " & ChrW(44592) & ChrW(47197) & ChrW(51012) & " " & ChrW(47784) & ChrW(46160) & " " & ChrW(52488) & ChrW(44592) & ChrW(54868) & ChrW(54616) & ChrW(49464) & ChrW(50836) & "?"
        Case "AskResetTitle"
            GetKoStr = ChrW(52488) & ChrW(44592) & ChrW(54868) & " " & ChrW(54869) & ChrW(51064)
    End Select
End Function
