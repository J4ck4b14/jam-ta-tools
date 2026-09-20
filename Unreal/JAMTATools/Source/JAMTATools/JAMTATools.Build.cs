using UnrealBuildTool;

public class JAMTATools : ModuleRules
{
    public JAMTATools(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

        PrivateDependencyModuleNames.AddRange(new[]
        {
            "Core",
            "CoreUObject",
            "Engine",
            "UnrealEd",
            "DataValidation",
            "Json"
        });
    }
}
