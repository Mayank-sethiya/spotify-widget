# Melo Box - A Feature-Rich Spotify Desktop Widget

Melo Box is a sleek, customizable desktop widget for Spotify that brings your music to life. Built with Python, it features a beautiful, modern UI, smooth animations, and powerful controls, making it the perfect companion for any music lover.

![Melo Box Demo](https://user-images.githubusercontent.com/12345/67890.gif) <!-- Placeholder for a cool GIF -->

## ✨ Features

- **Stunning UI**: A clean, modern interface with rounded corners and a blurred album art background.
- **Dynamic Startup Animation**: A unique, rainbow-colored text animation when the widget starts.
- **Customizable Icon & Sound**: Easily change the widget's icon and startup sound by placing files in the `assets` folder.
- **Recently Played Slideshow**: When music is idle, the widget displays a beautiful slideshow of your recently played album covers.
- **Full Playback Control**: Play, pause, and skip tracks directly from the widget.
- **Adaptive Resizing**: Intuitively resize the widget's width and height independently or together.
- **Adjustable Opacity**: Set the widget's transparency (100%, 85%, or 70%) to perfectly match your desktop setup.
- **Low-End PC Mode**: Automatically optimizes animations and effects for a smooth experience on all hardware.
- **Easy Setup**: A user-friendly, one-time setup window with clear instructions to get you started in minutes.

## 🚀 Setup Instructions

Follow these steps to get Melo Box up and running.

### 1. Download the Files
Download the `spotify_widget.py` and `requirements.txt` files into a new folder on your computer.

### 2. Create the `assets` Folder
In the same directory where you saved the script, create a new folder named `assets`. This is where you'll put your custom files.

```
/Melo Box/
├── assets/
├── spotify_widget.py
└── requirements.txt
```

### 3. Add Custom Icon and Sound (Optional)
- **Icon**: Place your desired icon file named `spotify_icon.png` inside the `assets` folder.
- **Sound**: Place your startup sound file named `startup.wav` inside the `assets` folder.

### 4. Install Dependencies
Open a terminal or command prompt in your project folder and run the following command to install the necessary Python libraries:

```bash
pip install -r requirements.txt
```

### 5. First Run & Configuration
Run the script for the first time from your terminal:
```bash
python spotify_widget.py
```
A setup window will appear with instructions on how to get your **Spotify Client ID**, **Client Secret**, and **Username**. Follow the on-screen guide to enter your credentials. This is a one-time setup.

## 🏃‍♂️ Running the Widget

To run Melo Box **without the black command prompt window**, simply rename the file:

- **From**: `spotify_widget.py`
- **To**: `spotify_widget.pyw`

Now, you can just double-click the `spotify_widget.pyw` file to launch it like a native application!

## ⚙️ Customization

Melo Box is designed to be yours. Here’s how you can customize it:

- **Opacity**: Right-click the widget and select `Set Opacity` to choose your preferred transparency level.
- **Startup Sound**: Right-click to enable or disable the startup sound for future launches.
- **Lock Position**: Right-click and select `Lock Position` to prevent the widget from being moved or resized.
- **Change Icon/Sound**: Simply replace the `spotify_icon.png` or `startup.wav` files in your `assets` folder.