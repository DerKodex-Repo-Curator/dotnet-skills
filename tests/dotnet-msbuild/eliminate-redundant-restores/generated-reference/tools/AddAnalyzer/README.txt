AddAnalyzer: a repo tool run by CI before the build.

  dotnet run --project tools/AddAnalyzer -- <project.csproj>

It edits <project.csproj> in place and adds
  <PackageReference Include="Microsoft.CodeAnalysis.NetAnalyzers" Version="10.0.100" PrivateAssets="all" />
when the project does not already reference it. It does not run restore.
