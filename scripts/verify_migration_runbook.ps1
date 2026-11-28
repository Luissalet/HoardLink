param([Parameter(Mandatory=$true)][string]$Path)

$ErrorActionPreference = 'Stop'
$Path = (Resolve-Path -LiteralPath $Path).Path
$Doc = [IO.File]::ReadAllText($Path)
$Headings = [regex]::Matches($Doc, '(?m)^##[ \t]+(?<step>\d+)\.')
$Fences = [regex]::Matches($Doc, '(?ms)^~~~powershell[ \t]*\r?\n(?<code>.*?)^~~~[ \t]*(?:\r?$)')
if (!$Fences.Count) { throw 'No PowerShell blocks found.' }

function Read-BlockAst([string]$Code, [string]$Label) {
    $Tokens = $null
    $ParseErrors = $null
    $Ast = [Management.Automation.Language.Parser]::ParseInput($Code, [ref]$Tokens, [ref]$ParseErrors)
    if ($ParseErrors.Count) { throw "$Label does not parse: $($ParseErrors[0].Message)" }
    $Ast
}

$Definitions = @{}
$Commands = [Collections.Generic.List[object]]::new()
$Problems = [Collections.Generic.List[string]]::new()
$StepZero = 0
$RollbackBlocks = 0
$PreviousStep = -1
for ($Index=0; $Index -lt $Fences.Count; $Index++) {
    $Fence = $Fences[$Index]
    $Heading = $Headings | Where-Object { $_.Index -lt $Fence.Index } | Select-Object -Last 1
    if (!$Heading) { throw "Block $Index is outside a numbered step." }
    $Step = [int]$Heading.Groups['step'].Value
    if ($Step -lt $PreviousStep) { throw "Step $Step is out of document order." }
    $PreviousStep = $Step
    $Label = "Step $Step, block $Index"
    $Ast = Read-BlockAst $Fence.Groups['code'].Value $Label
    if ($Step -eq 0) {
        $StepZero++
        if ($Index -ne 0 -or @($Ast.EndBlock.Statements | Where-Object {
            $_ -isnot [Management.Automation.Language.FunctionDefinitionAst]
        }).Count) {
            $Problems.Add("$Label must contain only top-level function declarations in the first block.")
        }
    }
    if ($Step -eq 10) { $RollbackBlocks++ }

    foreach ($Definition in $Ast.FindAll({
        param($Node) $Node -is [Management.Automation.Language.FunctionDefinitionAst]
    }, $true)) {
        if ($Definitions.ContainsKey($Definition.Name)) { $Problems.Add("Duplicate function $($Definition.Name).") }
        $Definitions[$Definition.Name] = @{step=$Step; block=$Index}
        if ($Step -ne 0 -or $Definition.Parent -ne $Ast.EndBlock) {
            $Problems.Add("$Label declares $($Definition.Name) outside block 0.")
        }
    }
    foreach ($Command in $Ast.FindAll({
        param($Node) $Node -is [Management.Automation.Language.CommandAst]
    }, $true)) {
        $Name = $Command.GetCommandName()
        if ($Name) { $Commands.Add(@{name=$Name; step=$Step; block=$Index}) }
    }
    foreach ($Launch in $Ast.FindAll({
        param($Node)
        $Node -is [Management.Automation.Language.AssignmentStatementAst] -and $Node.Left.Extent.Text -eq '$StartCode'
    }, $true)) {
        $Text = $Launch.Right.Expression
        if ($Text -isnot [Management.Automation.Language.StringConstantExpressionAst]) {
            $Problems.Add("$Label has an uncheckable Hub launch script.")
            continue
        }
        $ChildAst = Read-BlockAst $Text.Value "$Label launch script"
        if ($ChildAst.Find({
            param($Node) $Node -is [Management.Automation.Language.FunctionDefinitionAst]
        }, $true)) {
            $Problems.Add("$Label declares a function in the Hub launch script.")
        }
        foreach ($ChildCall in $ChildAst.FindAll({
            param($Node) $Node -is [Management.Automation.Language.CommandAst]
        }, $true)) {
            $ChildName = $ChildCall.GetCommandName()
            if ($ChildName) { $Commands.Add(@{name=$ChildName; step=$Step; block=$Index; isolated=$true}) }
        }
    }
}

if ($StepZero -ne 1 -or $RollbackBlocks -ne 1) { $Problems.Add('One block 0 and one step 10 recovery block are required.') }
$NativeCommands = @{}
$LocalCalls = 0
$RollbackHelpers = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($Call in $Commands) {
    $Def = $Definitions[$Call.name]
    if ($Def) {
        $LocalCalls++
        if ($Call.step -eq 10) { [void]$RollbackHelpers.Add($Call.name) }
        if ($Call.isolated) { $Problems.Add("The child launch script cannot call parent function $($Call.name).") }
        elseif ($Def.block -gt $Call.block) {
            $Problems.Add("Step $($Call.step) calls $($Call.name) before its declaration in step $($Def.step).")
        }
        continue
    }
    if (!$NativeCommands.ContainsKey($Call.name)) {
        # NetTCPIP and the PowerShell utility module also export system commands as functions.
        $NativeCommands[$Call.name] = [bool](Get-Command -Name $Call.name -All -CommandType Alias,Cmdlet,Application,Function -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandType -ne 'Function' -or $_.ModuleName -in ('NetTCPIP','Microsoft.PowerShell.Utility') })
    }
    if (!$NativeCommands[$Call.name]) { $Problems.Add("Step $($Call.step) calls undefined command $($Call.name).") }
}
if ($Problems.Count) { throw ($Problems -join [Environment]::NewLine) }
[pscustomobject]@{
    powershell_blocks = $Fences.Count
    functions_in_block_zero = $Definitions.Count
    named_local_calls = $LocalCalls
    direct_recovery_helpers = @($RollbackHelpers | Sort-Object)
}
