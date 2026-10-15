@echo off
rem Start SpaceClaim so that the AI bridge can attach to it (mode=attach).
rem
rem A plain double-click start does NOT work: such a SpaceClaim has no gRPC
rem endpoint at all, measured - its three listening ports all answered
rem UNAVAILABLE to a gRPC health check. Two settings are what make it attachable:
rem
rem   /ADDINMANIFESTFILE=...   loads the ApiServer add-in that serves gRPC
rem   API_PORT=51790           a fixed port the AI side can be told about
rem
rem Neither appears in the public Ansys command-line documentation. They come from
rem the PyAnsys Geometry source (product_instance.py:92, 113-117, 401-404) and from
rem measurement, so they are a SEMI-PUBLIC interface and a future release may
rem change them. See spaceclaim/docs/verification.md.
rem
rem If your ANSYS release is not v242, change both paths below (keep the quotes).
rem The AI side then runs: scdm_attach(host="localhost", port=51790)

set API_PORT=51790
set API_ADDRESS=localhost

start "" "C:\Program Files\ANSYS Inc\v242\scdm\SpaceClaim.exe" /ADDINMANIFESTFILE="C:\Program Files\ANSYS Inc\v242\Addins\ApiServer\Presentation.ApiServerAddIn.Manifest.xml" /transport-mode=wnua